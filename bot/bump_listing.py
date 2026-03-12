from __future__ import annotations

import logging
import re
from contextlib import suppress
from dataclasses import dataclass

from bot.captcha_detector import CaptchaDetectedError, ensure_no_captcha
from bot.logger import log_event
from bot.retry import build_async_retry
from bot.selectors import (
    ACCOUNT_MENU,
    EDIT_PHOTOS,
    LISTING_OPTIONS_MENU,
    LOGOUT_LINK,
    MY_LISTINGS,
    UPDATE_AND_VIEW,
    UPDATE_CONFIRMATION,
    click,
    listing_target,
    resolve,
    resolve_optional,
)


@dataclass(slots=True, frozen=True)
class BumpResult:
    successful_targets: tuple[str, ...]
    failed_targets: tuple[str, ...]


class ListingUpdateError(RuntimeError):
    pass


def _sanitize_target(target: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", target).strip("_")
    return normalized or "listing"


async def navigate_to_my_listings(browser, settings, logger):
    page = await browser.goto(settings.base_url, load_session=True)
    await ensure_no_captcha(page)

    my_listings = await resolve_optional(
        page,
        MY_LISTINGS,
        settings=settings,
        logger=logger,
        timeout_ms=4000,
    )
    if my_listings is None:
        await click(page, ACCOUNT_MENU, settings=settings, logger=logger)
        my_listings = await resolve(page, MY_LISTINGS, settings=settings, logger=logger)

    await my_listings.click()
    with suppress(Exception):
        await page.wait_for_load_state("domcontentloaded", timeout=settings.navigation_timeout_ms)
    await ensure_no_captcha(page)

    if await resolve_optional(page, LOGOUT_LINK, settings=settings, logger=logger, timeout_ms=2000) is None:
        log_event(
            logger,
            "my_listings_opened",
            status="success",
            component="bump_listing",
            note="Dashboard markers partially visible",
        )
    else:
        log_event(logger, "my_listings_opened", status="success", component="bump_listing")
    return page


async def open_listing_editor(page, settings, logger, target: str) -> None:
    target_locator = await resolve(
        page,
        listing_target(target),
        settings=settings,
        logger=logger,
        timeout_ms=settings.navigation_timeout_ms,
    )

    row = target_locator.locator(
        "xpath=ancestor::*[self::article or self::tr or self::li or .//button or .//a][1]"
    ).first

    edit_button = await resolve_optional(
        row,
        EDIT_PHOTOS,
        settings=settings,
        logger=logger,
        timeout_ms=4000,
    )

    if edit_button is None:
        options_button = await resolve_optional(
            row,
            LISTING_OPTIONS_MENU,
            settings=settings,
            logger=logger,
            timeout_ms=3000,
        )
        if options_button is not None:
            await options_button.click()
            edit_button = await resolve(page, EDIT_PHOTOS, settings=settings, logger=logger)

    if edit_button is None:
        await target_locator.click()
        with suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=settings.navigation_timeout_ms)
        edit_button = await resolve(page, EDIT_PHOTOS, settings=settings, logger=logger)

    await edit_button.click()
    with suppress(Exception):
        await page.wait_for_load_state("domcontentloaded", timeout=settings.navigation_timeout_ms)
    await ensure_no_captcha(page)
    await resolve(page, UPDATE_AND_VIEW, settings=settings, logger=logger)

    log_event(
        logger,
        "listing_editor_opened",
        status="success",
        component="bump_listing",
        target=target,
    )


async def submit_listing_update(page, settings, logger, target: str) -> None:
    update_button = await resolve(page, UPDATE_AND_VIEW, settings=settings, logger=logger)
    previous_url = page.url
    await update_button.click()
    with suppress(Exception):
        await page.wait_for_load_state("domcontentloaded", timeout=settings.navigation_timeout_ms)
    await ensure_no_captcha(page)

    confirmation = await resolve_optional(
        page,
        UPDATE_CONFIRMATION,
        settings=settings,
        logger=logger,
        timeout_ms=4000,
    )
    if confirmation is not None:
        await confirmation.click()
        with suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=settings.navigation_timeout_ms)
        await ensure_no_captcha(page)

    with suppress(Exception):
        await update_button.wait_for(state="hidden", timeout=settings.navigation_timeout_ms)

    still_visible = await resolve_optional(
        page,
        UPDATE_AND_VIEW,
        settings=settings,
        logger=logger,
        timeout_ms=2000,
    )
    if still_visible is not None and page.url == previous_url:
        raise ListingUpdateError(f"No post-update transition was observed for listing '{target}'.")

    log_event(
        logger,
        "listing_updated",
        status="success",
        component="bump_listing",
        target=target,
        final_url=page.url,
    )


async def _bump_target_once(browser, settings, logger, target: str) -> None:
    page = await navigate_to_my_listings(browser, settings, logger)
    await open_listing_editor(page, settings, logger, target)
    await submit_listing_update(page, settings, logger, target)
    await browser.save_session()


async def bump_targets(browser, settings, logger) -> BumpResult:
    successful_targets: list[str] = []
    failed_targets: list[str] = []

    for target in settings.listing_targets:
        retrying = build_async_retry(
            settings,
            excluded_exceptions=(CaptchaDetectedError,),
        )

        try:
            async for attempt in retrying:
                with attempt:
                    try:
                        await _bump_target_once(browser, settings, logger, target)
                        successful_targets.append(target)
                        break
                    except Exception as exc:
                        screenshot = await browser.capture_screenshot(
                            f"bump_failure_{_sanitize_target(target)}"
                        )
                        log_event(
                            logger,
                            "listing_bump_attempt_failed",
                            status="retrying",
                            component="bump_listing",
                            level=logging.ERROR,
                            error=str(exc),
                            target=target,
                            attempt=attempt.retry_state.attempt_number,
                            screenshot=str(screenshot) if screenshot else None,
                        )
                        await browser.restart(load_session=settings.storage_state_path.exists())
                        raise
        except CaptchaDetectedError:
            raise
        except Exception as exc:
            failed_targets.append(target)
            log_event(
                logger,
                "listing_bump_failed",
                status="error",
                component="bump_listing",
                level=logging.ERROR,
                error=str(exc),
                target=target,
            )

    status = "success" if not failed_targets else "partial_failure"
    log_event(
        logger,
        "bump_cycle_completed",
        status=status,
        component="bump_listing",
        successful_targets=successful_targets,
        failed_targets=failed_targets,
    )
    return BumpResult(tuple(successful_targets), tuple(failed_targets))
