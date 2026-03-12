from __future__ import annotations

import logging
from contextlib import suppress

from playwright.async_api import Page

from bot.captcha_detector import CaptchaDetectedError, ensure_no_captcha
from bot.logger import log_event
from bot.retry import build_async_retry
from bot.selectors import (
    ACCOUNT_MENU,
    COOKIE_ACCEPT,
    LOGIN_BUTTON,
    LOGIN_EMAIL,
    LOGIN_ERROR,
    LOGIN_PASSWORD,
    LOGOUT_LINK,
    MY_LISTINGS,
    REMEMBER_ME,
    click,
    click_optional,
    extract_text_optional,
    fill,
    is_visible,
    resolve,
    resolve_optional,
    wait_for_any,
)


class LoginFailedError(RuntimeError):
    pass


AUTH_GROUPS = (MY_LISTINGS, LOGOUT_LINK)


async def dismiss_cookie_banner(page: Page, settings, logger) -> None:
    if await click_optional(page, COOKIE_ACCEPT, settings=settings, logger=logger):
        log_event(logger, "cookie_banner_dismissed", status="success", component="login")


async def _open_account_menu(page: Page, settings, logger) -> None:
    if await is_visible(page, LOGIN_EMAIL, settings=settings, logger=logger, timeout_ms=2000):
        return

    await click(page, ACCOUNT_MENU, settings=settings, logger=logger)
    await wait_for_any(
        page,
        (*AUTH_GROUPS, LOGIN_EMAIL, LOGIN_PASSWORD),
        settings=settings,
        logger=logger,
        timeout_ms=settings.action_timeout_ms,
    )


async def is_authenticated(page: Page, settings, logger) -> bool:
    if await wait_for_any(page, AUTH_GROUPS, settings=settings, logger=logger, timeout_ms=2500):
        return True

    account_menu = await resolve_optional(page, ACCOUNT_MENU, settings=settings, logger=logger, timeout_ms=2500)
    if account_menu is None:
        return False

    with suppress(Exception):
        await account_menu.click()

    match = await wait_for_any(
        page,
        (*AUTH_GROUPS, LOGIN_EMAIL),
        settings=settings,
        logger=logger,
        timeout_ms=4000,
    )
    return bool(match and match[0].name in {MY_LISTINGS.name, LOGOUT_LINK.name})


async def _extract_login_error(page: Page, settings, logger) -> str:
    error_text = await extract_text_optional(
        page,
        LOGIN_ERROR,
        settings=settings,
        logger=logger,
        timeout_ms=3000,
    )
    if error_text:
        return error_text
    return "Login did not complete successfully."


async def _perform_login_once(browser, settings, logger) -> Page:
    page = await browser.goto(settings.base_url, load_session=settings.storage_state_path.exists())
    await dismiss_cookie_banner(page, settings, logger)
    await ensure_no_captcha(page)

    if await is_authenticated(page, settings, logger):
        log_event(logger, "session_reused", status="success", component="login")
        return page

    if settings.storage_state_path.exists():
        browser.clear_session()
        await browser.restart(load_session=False)
        page = await browser.goto(settings.base_url, load_session=False)
        await dismiss_cookie_banner(page, settings, logger)
        await ensure_no_captcha(page)

    await _open_account_menu(page, settings, logger)
    await fill(page, LOGIN_EMAIL, settings.email, settings=settings, logger=logger)
    await fill(page, LOGIN_PASSWORD, settings.password, settings=settings, logger=logger)

    remember_me = await resolve_optional(page, REMEMBER_ME, settings=settings, logger=logger, timeout_ms=2000)
    if remember_me is not None:
        with suppress(Exception):
            await remember_me.check()

    login_button = await resolve(page, LOGIN_BUTTON, settings=settings, logger=logger)
    await login_button.click()
    with suppress(Exception):
        await page.wait_for_load_state("domcontentloaded", timeout=settings.navigation_timeout_ms)

    await ensure_no_captcha(page)
    post_login = await wait_for_any(
        page,
        (*AUTH_GROUPS, LOGIN_ERROR, LOGIN_EMAIL),
        settings=settings,
        logger=logger,
        timeout_ms=settings.navigation_timeout_ms,
    )

    if post_login and post_login[0].name in {MY_LISTINGS.name, LOGOUT_LINK.name}:
        session_file = await browser.save_session()
        log_event(
            logger,
            "login_succeeded",
            status="success",
            component="login",
            session_file=str(session_file),
        )
        return page

    if await is_authenticated(page, settings, logger):
        session_file = await browser.save_session()
        log_event(
            logger,
            "login_succeeded",
            status="success",
            component="login",
            session_file=str(session_file),
        )
        return page

    error_text = await _extract_login_error(page, settings, logger)
    raise LoginFailedError(error_text)


async def ensure_authenticated(browser, settings, logger) -> Page:
    retrying = build_async_retry(
        settings,
        excluded_exceptions=(LoginFailedError, CaptchaDetectedError),
    )

    async for attempt in retrying:
        with attempt:
            try:
                return await _perform_login_once(browser, settings, logger)
            except Exception as exc:
                screenshot = await browser.capture_screenshot("login_failure")
                log_event(
                    logger,
                    "login_attempt_failed",
                    status="retrying",
                    component="login",
                    level=logging.ERROR,
                    error=str(exc),
                    attempt=attempt.retry_state.attempt_number,
                    screenshot=str(screenshot) if screenshot else None,
                )
                await browser.restart(load_session=False)
                raise

    raise RuntimeError("Authentication retries exhausted.")
