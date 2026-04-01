from __future__ import annotations

import logging
import re
from contextlib import suppress
from dataclasses import dataclass
from time import perf_counter

from bot.captcha_detector import CaptchaDetectedError, ensure_no_captcha
from bot.config import Settings, load_settings
from bot.logger import log_event
from bot.retry import build_async_retry
from bot.selectors import (
    ACCOUNT_MENU,
    COOKIE_ACCEPT,
    EDIT_PHOTOS,
    LISTING_OPTIONS_MENU,
    LOGOUT_LINK,
    MY_LISTINGS,
    UPDATE_AND_VIEW,
    UPDATE_CONFIRMATION,
    click,
    click_optional,
    listing_target,
    resolve,
    resolve_optional,
)
from bot.session_manager import (
    SessionManagerError,
    get_stored_or_refreshed_session,
    login_via_api,
    open_authenticated_context,
    open_context_from_session,
    save_session,
)


@dataclass(slots=True, frozen=True)
class BumpResult:
    successful_targets: tuple[str, ...]
    failed_targets: tuple[str, ...]


class ListingUpdateError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class BrowserBumpOutcome:
    listing_id: str
    success: bool
    attempts: int
    status_code: int | None
    latency_ms: float | None
    reason: str
    dry_run: bool


LISTING_EDITOR_URL_TEMPLATE = "https://www.wg-gesucht.de/angebot-bearbeiten.html?action=update_offer&offer_id={listing_id}"


def _sanitize_target(target: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", target).strip("_")
    return normalized or "listing"


def _listing_editor_url(listing_id: str) -> str:
    return LISTING_EDITOR_URL_TEMPLATE.format(listing_id=listing_id)


async def _dismiss_blocking_modals(page, logger) -> None:
    removed = await page.evaluate(
        """() => {
            let removed = 0;
            const selectors = [
                "#cmpbox",
                "#cmpbox2",
                ".cmpbox",
                ".cmpboxBG",
                "#private_users_ad_modal",
                ".campaign_display.modal",
                ".modal-backdrop",
            ];
            for (const selector of selectors) {
                for (const element of document.querySelectorAll(selector)) {
                    element.remove();
                    removed += 1;
                }
            }
            document.body.classList.remove("modal-open");
            document.body.style.removeProperty("padding-right");
            return removed;
        }"""
    )
    if removed:
        log_event(
            logger,
            "blocking_modal_dismissed",
            status="success",
            component="bump_listing",
            removed_elements=removed,
        )


async def _stabilize_editor_state(page, logger) -> None:
    for _ in range(3):
        await _dismiss_blocking_modals(page, logger)
        await page.wait_for_timeout(150)


async def _click_with_overlay_retries(locator, page, logger, *, timeout_ms: int, attempts: int = 3) -> None:
    last_error: Exception | None = None
    for attempt in range(attempts):
        await _stabilize_editor_state(page, logger)
        try:
            await locator.click(timeout=timeout_ms)
            return
        except Exception as exc:
            last_error = exc
            if attempt == attempts - 1:
                raise
    if last_error is not None:
        raise last_error


async def _find_confirmation(page, settings, logger):
    await _stabilize_editor_state(page, logger)
    dialogs = page.locator("[role='dialog'], .modal.in, .modal.show")
    with suppress(Exception):
        visible_dialog = await resolve_optional(
            dialogs,
            UPDATE_CONFIRMATION,
            settings=settings,
            logger=logger,
            timeout_ms=2500,
        )
        if visible_dialog is not None:
            return visible_dialog

    return await resolve_optional(
        page,
        UPDATE_CONFIRMATION,
        settings=settings,
        logger=logger,
        timeout_ms=2500,
    )


async def _open_my_listings(page, settings, logger) -> None:
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


async def navigate_to_my_listings(browser, settings, logger):
    page = await browser.goto(settings.base_url, load_session=True)
    await _open_my_listings(page, settings, logger)
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


async def _open_listing_editor_direct(page, settings, logger, listing_id: str, *, source: str) -> None:
    await page.goto(
        _listing_editor_url(listing_id),
        wait_until="domcontentloaded",
        timeout=settings.navigation_timeout_ms,
    )
    if await click_optional(page, COOKIE_ACCEPT, settings=settings, logger=logger, timeout_ms=2000):
        log_event(logger, "cookie_banner_dismissed", status="success", component="bump_listing")
    await ensure_no_captcha(page)
    await _stabilize_editor_state(page, logger)

    update_button = await resolve_optional(
        page,
        UPDATE_AND_VIEW,
        settings=settings,
        logger=logger,
        timeout_ms=5000,
    )
    if update_button is None:
        raise SessionManagerError(
            f"Listing editor did not load for listing '{listing_id}' via {source}. Current URL: {page.url}"
        )

    log_event(
        logger,
        "listing_editor_opened",
        status="success",
        component="bump_listing",
        target=listing_id,
        source=source,
        direct_navigation=True,
    )


async def _open_editor_page_for_listing(listing_id: str, settings, logger):
    last_error: Exception | None = None

    stored_session = await get_stored_or_refreshed_session(settings=settings, logger=logger)
    if stored_session is not None:
        browser_session = await open_context_from_session(stored_session, settings=settings, logger=logger)
        try:
            await _open_listing_editor_direct(
                browser_session.page,
                settings,
                logger,
                listing_id,
                source="stored_session",
            )
            return browser_session
        except Exception as exc:
            last_error = exc
            log_event(
                logger,
                "listing_editor_direct_open_failed",
                status="warning",
                component="bump_listing",
                level=logging.WARNING,
                error=str(exc),
                target=listing_id,
                source="stored_session",
            )
            await browser_session.close()

    with suppress(SessionManagerError):
        api_session = await login_via_api(settings=settings, logger=logger)
        save_session(api_session, settings=settings)
        browser_session = await open_context_from_session(api_session, settings=settings, logger=logger)
        try:
            await _open_listing_editor_direct(
                browser_session.page,
                settings,
                logger,
                listing_id,
                source="api_login",
            )
            return browser_session
        except Exception as exc:
            if isinstance(exc, CaptchaDetectedError):
                await browser_session.close()
                raise
            last_error = exc
            log_event(
                logger,
                "listing_editor_direct_open_failed",
                status="warning",
                component="bump_listing",
                level=logging.WARNING,
                error=str(exc),
                target=listing_id,
                source="api_login",
            )
            await browser_session.close()

    browser_session = await open_authenticated_context(settings=settings, logger=logger)
    try:
        if last_error is not None:
            log_event(
                logger,
                "listing_editor_fallback_browser_login",
                status="warning",
                component="bump_listing",
                error=str(last_error),
                target=listing_id,
            )
        await _open_my_listings(browser_session.page, settings, logger)
        await open_listing_editor(browser_session.page, settings, logger, listing_id)
        return browser_session
    except Exception as exc:
        await browser_session.close()
        raise exc


async def submit_listing_update(page, settings, logger, target: str) -> None:
    await _stabilize_editor_state(page, logger)
    update_button = await resolve(page, UPDATE_AND_VIEW, settings=settings, logger=logger)
    previous_url = page.url
    await _click_with_overlay_retries(
        update_button,
        page,
        logger,
        timeout_ms=settings.action_timeout_ms,
    )
    with suppress(Exception):
        await page.wait_for_load_state("domcontentloaded", timeout=settings.navigation_timeout_ms)
    await ensure_no_captcha(page)
    await _stabilize_editor_state(page, logger)

    confirmation = await _find_confirmation(page, settings, logger)
    if confirmation is not None:
        await _click_with_overlay_retries(
            confirmation,
            page,
            logger,
            timeout_ms=settings.action_timeout_ms,
        )
        with suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=settings.navigation_timeout_ms)
        await ensure_no_captcha(page)
        await _stabilize_editor_state(page, logger)

    with suppress(Exception):
        await update_button.wait_for(state="hidden", timeout=settings.navigation_timeout_ms)
    await _stabilize_editor_state(page, logger)

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


async def bump_listing_via_browser(
    listing_id: str,
    *,
    settings: Settings | None = None,
    logger=None,
) -> BrowserBumpOutcome:
    settings = settings or load_settings()
    logger = logger or logging.getLogger("wg_bump_bot")
    retrying = build_async_retry(
        settings,
        attempts=min(settings.retry_attempts, 5),
        excluded_exceptions=(CaptchaDetectedError, SessionManagerError),
    )

    async for attempt in retrying:
        with attempt:
            started_at = perf_counter()
            browser_session = await _open_editor_page_for_listing(listing_id, settings, logger)
            try:
                if settings.dry_run:
                    reason = "Dry run confirmed the update flow without submitting the listing."
                    log_event(
                        logger,
                        "listing_update_dry_run",
                        status="success",
                        component="bump_listing",
                        listing_id=listing_id,
                    )
                else:
                    await submit_listing_update(browser_session.page, settings, logger, listing_id)
                    reason = "Listing updated through the browser flow."

                latency_ms = (perf_counter() - started_at) * 1000
                return BrowserBumpOutcome(
                    listing_id=listing_id,
                    success=True,
                    attempts=attempt.retry_state.attempt_number,
                    status_code=None,
                    latency_ms=latency_ms,
                    reason=reason,
                    dry_run=settings.dry_run,
                )
            except Exception as exc:
                log_event(
                    logger,
                    "listing_bump_attempt_failed",
                    status="retrying",
                    component="bump_listing",
                    level=logging.ERROR,
                    error=str(exc),
                    target=listing_id,
                    attempt=attempt.retry_state.attempt_number,
                )
                raise
            finally:
                await browser_session.close()

    raise ListingUpdateError(f"Failed to update listing '{listing_id}'.")
