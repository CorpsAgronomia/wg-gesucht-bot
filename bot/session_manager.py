from __future__ import annotations

import json
import logging
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from playwright.async_api import Browser, BrowserContext, Locator, Page, Playwright, async_playwright

from bot.captcha_detector import CaptchaDetectedError, ensure_no_captcha
from bot.config import Settings, load_settings
from bot.logger import log_event
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
    fill,
    resolve_optional,
    wait_for_any,
)


AUTH_MARKERS = (MY_LISTINGS, LOGOUT_LINK)
CSRF_COOKIE_NAMES = ("csrf", "xsrf", "_csrf", "token")
CSRF_SELECTORS = (
    "meta[name='csrf-token']",
    "meta[name='_csrf']",
    "meta[name='csrf']",
    "input[name='csrf']",
    "input[name='_csrf']",
    "input[name='csrf_token']",
    "input[name='xsrf_token']",
)
REFRESH_SESSION_URL = "https://www.wg-gesucht.de/ajax/sessions.php?action=refresh_tokens"
LOGIN_URL = "https://www.wg-gesucht.de/ajax/sessions.php?action=login"
DEFAULT_CLIENT_ID = "wg_desktop_website"
DEFAULT_SMP_CLIENT = "WG-Gesucht"


class SessionManagerError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class SessionData:
    cookies: list[dict[str, Any]]
    csrf_token: str
    user_agent: str
    captured_at: str
    access_token: str = ""
    refresh_token: str = ""
    client_id: str = ""
    dev_ref_no: str = ""
    user_id: str = ""
    login_token: str = ""


@dataclass(slots=True)
class AuthenticatedBrowserSession:
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page

    async def close(self) -> None:
        with suppress(Exception):
            await self.page.close()
        with suppress(Exception):
            await self.context.close()
        with suppress(Exception):
            await self.browser.close()
        with suppress(Exception):
            await self.playwright.stop()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _get_logger(logger):
    return logger or logging.getLogger("wg_bump_bot")


def _cookie_value(cookies: list[dict[str, Any]], *names: str) -> str:
    expected = {name.lower() for name in names}
    for cookie in cookies:
        name = str(cookie.get("name", "")).lower()
        if name in expected:
            return str(cookie.get("value", "")).strip()
    return ""


def _cookies_to_httpx(cookies: list[dict[str, Any]]) -> httpx.Cookies:
    jar = httpx.Cookies()
    for cookie in cookies:
        name = str(cookie.get("name", "")).strip()
        value = str(cookie.get("value", ""))
        if not name:
            continue

        kwargs: dict[str, Any] = {}
        if cookie.get("domain"):
            kwargs["domain"] = str(cookie["domain"])
        if cookie.get("path"):
            kwargs["path"] = str(cookie["path"])
        jar.set(name, value, **kwargs)
    return jar


def _serialize_cookies(cookies: httpx.Cookies) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for cookie in cookies.jar:
        serialized.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "expires": cookie.expires,
                "httpOnly": "HttpOnly" in getattr(cookie, "_rest", {}),
                "secure": bool(cookie.secure),
                "sameSite": getattr(cookie, "_rest", {}).get("SameSite"),
            }
        )
    return serialized


def _parse_refresh_payload(response: httpx.Response) -> dict[str, Any]:
    body = response.text.strip()
    if not body:
        return {}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SessionManagerError("Token refresh returned an invalid response body.") from exc

    if not isinstance(payload, dict):
        raise SessionManagerError("Token refresh returned an invalid payload.")

    if payload.get("detail") is None:
        return payload

    detail = payload.get("detail")
    if not isinstance(detail, dict):
        raise SessionManagerError("Token refresh response is missing detail data.")

    return detail


def _build_api_headers(
    *,
    user_agent: str,
    client_id: str,
    access_token: str = "",
    dev_ref_no: str = "",
    user_id: str = "",
    referer: str,
) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": referer,
        "User-Agent": user_agent,
        "X-Client-Id": client_id or DEFAULT_CLIENT_ID,
        "X-Requested-With": "XMLHttpRequest",
        "X-Smp-Client": DEFAULT_SMP_CLIENT,
    }
    if access_token:
        headers["X-Authorization"] = f"Bearer {access_token}"
    if dev_ref_no:
        headers["X-Dev-Ref-No"] = dev_ref_no
    if user_id:
        headers["X-User-Id"] = user_id
    return headers


async def _dismiss_cookie_banner(page: Page, settings: Settings, logger) -> None:
    if await click_optional(page, COOKIE_ACCEPT, settings=settings, logger=logger, timeout_ms=2000):
        log_event(logger, "cookie_banner_dismissed", status="success", component="session")


async def _capture_debug_screenshot(page: Page, settings: Settings, prefix: str) -> str | None:
    settings.screenshots_dir.mkdir(parents=True, exist_ok=True)
    path = settings.screenshots_dir / f"{prefix}_{_timestamp_slug()}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
    except Exception:
        return None
    return str(path)


async def _is_authenticated(page: Page, settings: Settings, logger) -> bool:
    if await wait_for_any(page, AUTH_MARKERS, settings=settings, logger=logger, timeout_ms=2500):
        return True

    account_menu = await resolve_optional(page, ACCOUNT_MENU, settings=settings, logger=logger, timeout_ms=2500)
    if account_menu is None:
        return False

    with suppress(Exception):
        await account_menu.click()

    match = await wait_for_any(
        page,
        (*AUTH_MARKERS, LOGIN_EMAIL),
        settings=settings,
        logger=logger,
        timeout_ms=3000,
    )
    return bool(match and match[0].name in {MY_LISTINGS.name, LOGOUT_LINK.name})


async def _extract_login_error(page: Page, settings: Settings, logger) -> str:
    locator = await resolve_optional(page, LOGIN_ERROR, settings=settings, logger=logger, timeout_ms=2000)
    if locator is None:
        return "Login did not complete successfully."
    text = (await locator.inner_text()).strip()
    return text or "Login did not complete successfully."


async def _resolve_login_fields(page: Page, settings: Settings, logger, *, timeout_ms: int) -> tuple[Locator, Locator] | None:
    email = await resolve_optional(page, LOGIN_EMAIL, settings=settings, logger=logger, timeout_ms=timeout_ms)
    password = await resolve_optional(page, LOGIN_PASSWORD, settings=settings, logger=logger, timeout_ms=timeout_ms)
    if email is None or password is None:
        return None
    return email, password


async def _open_login_form(page: Page, settings: Settings, logger) -> bool:
    existing_fields = await _resolve_login_fields(page, settings, logger, timeout_ms=2000)
    if existing_fields is not None:
        log_event(logger, "login_form_visible", status="success", component="session", source="already_open")
        return True

    for attempt in range(1, 3):
        with suppress(Exception):
            await click(page, ACCOUNT_MENU, settings=settings, logger=logger)
            log_event(
                logger,
                "account_menu_clicked",
                status="success",
                component="session",
                attempt=attempt,
            )

        match = await wait_for_any(
            page,
            (*AUTH_MARKERS, LOGIN_EMAIL, LOGIN_PASSWORD),
            settings=settings,
            logger=logger,
            timeout_ms=settings.action_timeout_ms,
        )
        if match and match[0].name in {MY_LISTINGS.name, LOGOUT_LINK.name}:
            log_event(
                logger,
                "session_reused",
                status="success",
                component="session",
                source="account_menu",
                attempt=attempt,
            )
            return False

        fields = await _resolve_login_fields(page, settings, logger, timeout_ms=2000)
        if fields is not None:
            log_event(
                logger,
                "login_form_ready",
                status="success",
                component="session",
                attempt=attempt,
            )
            return True

    raise SessionManagerError("Login form did not expose both email and password fields.")


async def _extract_csrf_token(page: Page, cookies: list[dict[str, Any]]) -> str:
    for selector in CSRF_SELECTORS:
        locator = page.locator(selector).first
        with suppress(Exception):
            if await locator.count():
                value = await locator.get_attribute("content")
                if not value:
                    value = await locator.get_attribute("value")
                if value:
                    return value.strip()

    with suppress(Exception):
        token = await page.evaluate(
            """() => {
                const candidates = [
                    window.csrfToken,
                    window.CSRF_TOKEN,
                    window.__csrf,
                    document.querySelector("meta[name='csrf-token']")?.content,
                ];
                return candidates.find((value) => typeof value === "string" && value.trim()) || "";
            }"""
        )
        if token:
            return str(token).strip()

    for cookie in cookies:
        name = str(cookie.get("name", "")).lower()
        if any(marker in name for marker in CSRF_COOKIE_NAMES):
            value = str(cookie.get("value", "")).strip()
            if value:
                return value

    return ""


async def _extract_user_id(page: Page) -> str:
    patterns = (
        r"/users/(\d{6,})",
        r"/profile-images/(\d{6,})",
        r"user_id\s*=\s*['\"](\d{6,})",
        r"userId\s*[:=]\s*['\"]?(\d{6,})",
        r'"user(?:_id|Id)"\s*[:=]\s*"?(\\d{6,})',
        r"data-user-id=['\"](\d{6,})",
    )
    with suppress(Exception):
        return await page.evaluate(
            """(patterns) => {
                const html = document.documentElement.outerHTML;
                for (const source of patterns) {
                    const regex = new RegExp(source, "i");
                    const match = html.match(regex);
                    if (match && match[1]) {
                        return match[1];
                    }
                }
                return "";
            }""",
            list(patterns),
        )
    return ""


async def open_authenticated_context(
    settings: Settings | None = None,
    logger=None,
    *,
    headless: bool | None = None,
) -> AuthenticatedBrowserSession:
    settings = settings or load_settings()
    logger = _get_logger(logger)

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(
        headless=settings.headless if headless is None else headless,
        slow_mo=settings.slow_mo_ms,
        args=["--disable-dev-shm-usage"],
    )
    context = await browser.new_context(
        user_agent=settings.user_agent,
        viewport={"width": settings.viewport_width, "height": settings.viewport_height},
        locale=settings.locale,
        timezone_id=settings.timezone,
    )
    context.set_default_timeout(settings.action_timeout_ms)
    context.set_default_navigation_timeout(settings.navigation_timeout_ms)
    page = await context.new_page()

    try:
        await page.goto(settings.base_url, wait_until="domcontentloaded", timeout=settings.navigation_timeout_ms)
        await _dismiss_cookie_banner(page, settings, logger)
        await ensure_no_captcha(page)

        if not await _is_authenticated(page, settings, logger):
            login_required = await _open_login_form(page, settings, logger)
            if login_required and not await _is_authenticated(page, settings, logger):
                await fill(page, LOGIN_EMAIL, settings.email, settings=settings, logger=logger)
                await fill(page, LOGIN_PASSWORD, settings.password, settings=settings, logger=logger)
                log_event(logger, "login_credentials_filled", status="success", component="session")

                remember_me = await resolve_optional(
                    page,
                    REMEMBER_ME,
                    settings=settings,
                    logger=logger,
                    timeout_ms=2000,
                )
                if remember_me is not None:
                    with suppress(Exception):
                        await remember_me.check()

                await click(page, LOGIN_BUTTON, settings=settings, logger=logger)
                log_event(logger, "login_submitted", status="success", component="session")
                with suppress(Exception):
                    await page.wait_for_load_state("domcontentloaded", timeout=settings.navigation_timeout_ms)

        await ensure_no_captcha(page)

        if await _is_authenticated(page, settings, logger):
            log_event(logger, "login_succeeded", status="success", component="session")
            return AuthenticatedBrowserSession(
                playwright=playwright,
                browser=browser,
                context=context,
                page=page,
            )

        raise SessionManagerError(await _extract_login_error(page, settings, logger))
    except Exception as exc:
        screenshot = await _capture_debug_screenshot(page, settings, "session_failure")
        log_event(
            logger,
            "session_browser_flow_failed",
            status="error",
            component="session",
            level=logging.ERROR,
            error=str(exc),
            screenshot=screenshot,
        )
        with suppress(Exception):
            await page.close()
        with suppress(Exception):
            await context.close()
        with suppress(Exception):
            await browser.close()
        with suppress(Exception):
            await playwright.stop()
        raise


async def login_and_capture_session(
    settings: Settings | None = None,
    logger=None,
    *,
    headless: bool | None = None,
) -> SessionData:
    settings = settings or load_settings()
    logger = _get_logger(logger)
    browser_session = await open_authenticated_context(settings=settings, logger=logger, headless=headless)
    try:
        cookies = await browser_session.context.cookies()
        csrf_token = await _extract_csrf_token(browser_session.page, cookies)
        session = SessionData(
            cookies=cookies,
            csrf_token=csrf_token,
            user_agent=settings.user_agent,
            captured_at=_utc_now(),
            access_token=_cookie_value(cookies, "X-Access-Token"),
            refresh_token=_cookie_value(cookies, "X-Refresh-Token"),
            client_id=_cookie_value(cookies, "X-Client-Id"),
            dev_ref_no=_cookie_value(cookies, "X-Dev-Ref-No", "dev_ref_no"),
            user_id=await _extract_user_id(browser_session.page),
            login_token=_cookie_value(cookies, "login_token"),
        )
        log_event(
            logger,
            "session_captured",
            status="success",
            component="session",
            cookie_count=len(cookies),
            csrf_token_present=bool(csrf_token),
            user_id_present=bool(session.user_id),
            access_token_present=bool(session.access_token),
        )
        return session
    finally:
        await browser_session.close()


async def login_via_api(
    *,
    settings: Settings,
    logger,
) -> SessionData:
    async with httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": settings.user_agent},
    ) as client:
        bootstrap_response = await client.get(settings.base_url)
        if bootstrap_response.status_code >= 400:
            raise SessionManagerError(f"Failed to bootstrap WG-Gesucht session with HTTP {bootstrap_response.status_code}.")

        client_id = client.cookies.get("X-Client-Id") or DEFAULT_CLIENT_ID
        dev_ref_no = client.cookies.get("X-Dev-Ref-No") or client.cookies.get("dev_ref_no") or ""
        access_token = client.cookies.get("X-Access-Token") or ""
        headers = _build_api_headers(
            user_agent=settings.user_agent,
            client_id=client_id,
            access_token=access_token,
            dev_ref_no=dev_ref_no,
            referer=settings.base_url,
        )
        payload = {
            "login_email_username": settings.email,
            "login_password": settings.password,
            "login_form_auto_login": "1",
            "csrf_token": "",
        }
        response = await client.post(
            LOGIN_URL,
            headers=headers,
            content=json.dumps(payload),
        )

        if response.status_code == 202:
            raise SessionManagerError("Two-factor authentication is required for this account.")
        if response.status_code in {400, 401}:
            raise SessionManagerError("WG-Gesucht rejected the supplied login credentials.")
        if response.status_code >= 400:
            raise SessionManagerError(f"API login failed with HTTP {response.status_code}.")

        try:
            detail = json.loads(response.text or "{}")
        except json.JSONDecodeError as exc:
            raise SessionManagerError("API login returned an invalid response body.") from exc

        if not isinstance(detail, dict):
            raise SessionManagerError("API login returned an invalid payload.")

        cookies = _serialize_cookies(client.cookies)
        session = SessionData(
            cookies=cookies,
            csrf_token=str(detail.get("csrf_token", "")).strip(),
            user_agent=settings.user_agent,
            captured_at=_utc_now(),
            access_token=str(detail.get("access_token", "")).strip()
            or _cookie_value(cookies, "X-Access-Token"),
            refresh_token=str(detail.get("refresh_token", "")).strip()
            or _cookie_value(cookies, "X-Refresh-Token"),
            client_id=client_id,
            dev_ref_no=str(detail.get("dev_ref_no", "")).strip()
            or _cookie_value(cookies, "X-Dev-Ref-No", "dev_ref_no"),
            user_id=str(detail.get("user_id", "")).strip(),
            login_token=_cookie_value(cookies, "login_token"),
        )

    if not session.access_token or not session.refresh_token or not session.user_id:
        raise SessionManagerError("API login did not return usable session tokens.")

    log_event(
        logger,
        "login_succeeded",
        status="success",
        component="session",
        method="api",
        cookie_count=len(session.cookies),
        csrf_token_present=bool(session.csrf_token),
        user_id_present=bool(session.user_id),
        access_token_present=bool(session.access_token),
    )
    return session


async def refresh_session_via_api(
    session: SessionData,
    *,
    settings: Settings,
    logger,
) -> SessionData:
    if not session.access_token or not session.refresh_token or not session.user_id or not session.dev_ref_no:
        raise SessionManagerError("Stored session does not contain enough data for token refresh.")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": "https://www.wg-gesucht.de/mein-wg-gesucht.html",
        "User-Agent": session.user_agent or settings.user_agent,
        "X-Authorization": f"Bearer {session.access_token}",
        "X-Client-Id": session.client_id or DEFAULT_CLIENT_ID,
        "X-Dev-Ref-No": session.dev_ref_no,
        "X-Requested-With": "XMLHttpRequest",
        "X-Smp-Client": "WG-Gesucht",
        "X-User-Id": session.user_id,
    }

    async with httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
        cookies=_cookies_to_httpx(session.cookies),
    ) as client:
        response = await client.put(REFRESH_SESSION_URL, headers=headers)
        if response.status_code >= 400:
            raise SessionManagerError(
                f"Token refresh failed with HTTP {response.status_code}."
            )

        detail = _parse_refresh_payload(response)
        refreshed_cookies = _serialize_cookies(client.cookies)
        refreshed_session = SessionData(
            cookies=refreshed_cookies,
            csrf_token=str(detail.get("csrf_token", "")).strip() or session.csrf_token,
            user_agent=session.user_agent or settings.user_agent,
            captured_at=_utc_now(),
            access_token=str(detail.get("access_token", "")).strip()
            or _cookie_value(refreshed_cookies, "X-Access-Token"),
            refresh_token=str(detail.get("refresh_token", "")).strip()
            or _cookie_value(refreshed_cookies, "X-Refresh-Token"),
            client_id=session.client_id or DEFAULT_CLIENT_ID,
            dev_ref_no=str(detail.get("dev_ref_no", "")).strip()
            or _cookie_value(refreshed_cookies, "X-Dev-Ref-No", "dev_ref_no"),
            user_id=str(detail.get("user_id", "")).strip() or session.user_id,
            login_token=_cookie_value(refreshed_cookies, "login_token") or session.login_token,
        )

    if not refreshed_session.access_token or not refreshed_session.csrf_token:
        raise SessionManagerError("Token refresh response did not contain usable session data.")

    log_event(
        logger,
        "session_refreshed",
        status="success",
        component="session",
        method="api",
        cookie_count=len(refreshed_session.cookies),
        csrf_token_present=bool(refreshed_session.csrf_token),
        user_id_present=bool(refreshed_session.user_id),
        access_token_present=bool(refreshed_session.access_token),
    )
    return refreshed_session


def save_session(
    session: SessionData,
    settings: Settings | None = None,
    path: Path | None = None,
) -> Path:
    resolved_settings = settings or (None if path is not None else load_settings())
    target = path or resolved_settings.session_file
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = target.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(asdict(session), ensure_ascii=True, indent=2), encoding="utf-8")
    temporary_path.replace(target)
    return target


def load_session(settings: Settings | None = None, path: Path | None = None) -> SessionData:
    resolved_settings = settings or (None if path is not None else load_settings())
    target = path or resolved_settings.session_file
    if not target.exists():
        raise FileNotFoundError(f"Session file not found: {target}")

    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SessionManagerError(f"Invalid session payload in {target}")

    cookies = payload.get("cookies")
    if not isinstance(cookies, list):
        raise SessionManagerError(f"Session file {target} does not contain a cookie list")

    return SessionData(
        cookies=cookies,
        csrf_token=str(payload.get("csrf_token", "")).strip(),
        user_agent=str(
            payload.get(
                "user_agent",
                getattr(resolved_settings, "user_agent", ""),
            )
        ).strip()
        or getattr(resolved_settings, "user_agent", ""),
        captured_at=str(payload.get("captured_at", "")),
        access_token=str(payload.get("access_token", "")).strip(),
        refresh_token=str(payload.get("refresh_token", "")).strip(),
        client_id=str(payload.get("client_id", "")).strip(),
        dev_ref_no=str(payload.get("dev_ref_no", "")).strip(),
        user_id=str(payload.get("user_id", "")).strip(),
        login_token=str(payload.get("login_token", "")).strip(),
    )


async def refresh_session(settings: Settings | None = None, logger=None) -> SessionData:
    settings = settings or load_settings()
    logger = _get_logger(logger)
    try:
        with suppress(FileNotFoundError, SessionManagerError):
            existing_session = load_session(settings=settings)
            session = await refresh_session_via_api(existing_session, settings=settings, logger=logger)
            save_session(session, settings=settings)
            return session

        with suppress(SessionManagerError):
            session = await login_via_api(settings=settings, logger=logger)
            save_session(session, settings=settings)
            return session

        session = await login_and_capture_session(settings=settings, logger=logger)
        save_session(session, settings=settings)
        return session
    except CaptchaDetectedError:
        raise
    except Exception as exc:
        log_event(
            logger,
            "session_refresh_failed",
            status="error",
            component="session",
            level=logging.ERROR,
            error=str(exc),
        )
        raise
