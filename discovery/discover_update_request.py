from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from playwright.async_api import Request

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.bump_listing import open_listing_editor
from bot.config import load_settings, prepare_runtime
from bot.logger import configure_logging, log_event, shutdown_logging
from bot.request_templates import should_use_legacy_template, template_path_for_listing
from bot.selectors import ACCOUNT_MENU, MY_LISTINGS, UPDATE_AND_VIEW, click, resolve_optional
from bot.session_manager import (
    SessionData,
    _cookie_value,
    _extract_csrf_token,
    _extract_user_id,
    open_authenticated_context,
    save_session,
)

ESSENTIAL_HEADERS = {
    "accept",
    "accept-language",
    "content-type",
    "origin",
    "referer",
    "x-requested-with",
    "x-authorization",
    "x-user-id",
    "x-client-id",
    "x-dev-ref-no",
    "x-smp-client",
}


def _placeholderize(value: Any, *, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        result = value
        for actual, placeholder in replacements.items():
            if actual:
                result = result.replace(actual, placeholder)
        return result
    if isinstance(value, list):
        return [_placeholderize(item, replacements=replacements) for item in value]
    if isinstance(value, dict):
        return {key: _placeholderize(item, replacements=replacements) for key, item in value.items()}
    return value


def _normalize_headers(headers: dict[str, str], *, replacements: dict[str, str]) -> dict[str, str]:
    filtered: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in ESSENTIAL_HEADERS or "csrf" in lowered:
            filtered[key] = _placeholderize(value, replacements=replacements)
    return filtered


def _extract_required_cookies(cookie_header: str) -> list[str]:
    names: list[str] = []
    for part in cookie_header.split(";"):
        candidate = part.partition("=")[0].strip()
        if candidate and candidate not in names:
            names.append(candidate)
    return names


def _build_body_template(
    payload: str,
    *,
    content_type: str,
    replacements: dict[str, str],
) -> tuple[dict[str, Any], str, str]:
    lowered = content_type.lower()
    if "application/x-www-form-urlencoded" in lowered:
        fields = {
            key: _placeholderize(value, replacements=replacements)
            for key, value in parse_qsl(payload, keep_blank_values=True)
        }
        csrf_body_field = next((key for key in fields if "csrf" in key.lower()), "")
        return {"encoding": "form", "fields": fields}, csrf_body_field, ""

    if "application/json" in lowered:
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return {
                "encoding": "raw",
                "raw": _placeholderize(payload, replacements=replacements),
            }, "", ""

        csrf_body_field = ""
        if isinstance(parsed, dict):
            csrf_body_field = next((key for key in parsed if "csrf" in key.lower()), "")
        return {
            "encoding": "json",
            "json": _placeholderize(parsed, replacements=replacements),
        }, csrf_body_field, ""

    if payload:
        return {
            "encoding": "raw",
            "raw": _placeholderize(payload, replacements=replacements),
        }, "", ""

    return {"encoding": "empty"}, "", ""


async def _score_request(request: Request, *, listing_id: str) -> int:
    headers = await request.all_headers()
    payload = request.post_data or ""
    url = request.url.lower()
    haystack = " ".join(
        [
            url,
            request.method.lower(),
            payload.lower(),
            json.dumps(headers, sort_keys=True).lower(),
        ]
    )

    score = 0
    if request.method.upper() in {"POST", "PUT", "PATCH"}:
        score += 6
    if request.method.upper() == "PUT":
        score += 4
    if request.resource_type in {"fetch", "xhr", "document"}:
        score += 2
    if "/api/offers/" in url:
        score += 8
    if "/api/images/" in url:
        score -= 4
    if "/users/" in url:
        score += 2
    if listing_id and listing_id in haystack:
        score += 5
    if "csrf" in haystack:
        score += 2
    if "x-authorization" in haystack:
        score += 2
    if any(token in haystack for token in ("aktual", "update", "anzeigen", "edit")):
        score += 1
    return score


async def _pick_request(captured_requests: list[Request], *, listing_id: str) -> Request | None:
    scored: list[tuple[int, int, Request]] = []
    for index, request in enumerate(captured_requests):
        score = await _score_request(request, listing_id=listing_id)
        if score > 0:
            scored.append((score, index, request))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]))
    return scored[-1][2]


async def _open_my_listings(page, settings, logger) -> None:
    my_listings = await resolve_optional(page, MY_LISTINGS, settings=settings, logger=logger, timeout_ms=3000)
    if my_listings is None:
        await click(page, ACCOUNT_MENU, settings=settings, logger=logger)
        my_listings = await resolve_optional(
            page,
            MY_LISTINGS,
            settings=settings,
            logger=logger,
            timeout_ms=settings.navigation_timeout_ms,
        )
    if my_listings is None:
        raise RuntimeError("Unable to locate 'Meine Anzeigen' after login.")
    await my_listings.click()
    with suppress(Exception):
        await page.wait_for_load_state("domcontentloaded", timeout=settings.navigation_timeout_ms)


async def _capture_request_template(
    request: Request,
    *,
    replacements: dict[str, str],
    validation_url: str,
) -> dict[str, Any]:
    headers = await request.all_headers()
    cookie_header = headers.get("cookie", headers.get("Cookie", ""))
    content_type = headers.get("content-type", headers.get("Content-Type", ""))
    payload = request.post_data or ""
    body_template, csrf_body_field, _ = _build_body_template(
        payload,
        content_type=content_type,
        replacements=replacements,
    )
    csrf_header_name = next((key for key in headers if "csrf" in key.lower()), "")
    csrf_field = csrf_body_field or csrf_header_name
    return {
        "endpoint": _placeholderize(request.url, replacements=replacements),
        "method": request.method.upper(),
        "headers": _normalize_headers(headers, replacements=replacements),
        "body_template": body_template,
        "required_cookies": _extract_required_cookies(cookie_header),
        "csrf_field": csrf_field,
        "csrf_header_name": csrf_header_name,
        "csrf_body_field": csrf_body_field,
        "validation_url_template": _placeholderize(validation_url, replacements=replacements),
    }


def _write_template_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )


async def discover_update_request(*, manual: bool, listing_id: str | None = None) -> dict[str, Any]:
    settings = load_settings()
    prepare_runtime(settings)
    logger = configure_logging(settings.logs_dir)
    browser_session = await open_authenticated_context(
        settings=settings,
        logger=logger,
        headless=False if manual else settings.headless,
    )

    try:
        cookies = await browser_session.context.cookies()
        csrf_token = await _extract_csrf_token(browser_session.page, cookies)
        session = SessionData(
            cookies=cookies,
            csrf_token=csrf_token,
            user_agent=settings.user_agent,
            captured_at=datetime.now(timezone.utc).isoformat(),
            access_token=_cookie_value(cookies, "X-Access-Token"),
            refresh_token=_cookie_value(cookies, "X-Refresh-Token"),
            client_id=_cookie_value(cookies, "X-Client-Id"),
            dev_ref_no=_cookie_value(cookies, "X-Dev-Ref-No", "dev_ref_no"),
            user_id=await _extract_user_id(browser_session.page),
            login_token=_cookie_value(cookies, "login_token"),
        )
        save_session(session, settings=settings)

        listing_id = (listing_id or settings.listing_ids[0]).strip()
        if listing_id not in settings.listing_ids:
            raise ValueError(
                f"Listing ID {listing_id} is not configured. Configured IDs: {', '.join(settings.listing_ids)}"
            )
        replacements = {
            listing_id: "{listing_id}",
            csrf_token: "{csrf_token}",
            session.access_token: "{access_token}",
            session.refresh_token: "{refresh_token}",
            session.client_id: "{client_id}",
            session.dev_ref_no: "{dev_ref_no}",
            session.user_id: "{user_id}",
            session.login_token: "{login_token}",
        }
        captured_requests: list[Request] = []
        browser_session.page.on("request", lambda request: captured_requests.append(request))

        if manual:
            print("Navigate to the target listing editor and click 'Aktualisieren und Ansehen'.")
            print("The script will keep listening until it detects the update request.")
            baseline = len(captured_requests)
            for _ in range(300):
                await asyncio.sleep(1)
                candidate = await _pick_request(captured_requests[baseline:], listing_id=listing_id)
                if candidate is not None:
                    validation_url = browser_session.page.url
                    template = await _capture_request_template(
                        candidate,
                        replacements=replacements,
                        validation_url=validation_url,
                    )
                    _write_template_file(template_path_for_listing(settings, listing_id), template)
                    if should_use_legacy_template(settings, listing_id):
                        _write_template_file(settings.request_template_path, template)
                    return template
            raise RuntimeError("Timed out waiting for the update request in manual mode.")

        await _open_my_listings(browser_session.page, settings, logger)
        await open_listing_editor(browser_session.page, settings, logger, listing_id)
        validation_url = browser_session.page.url
        baseline = len(captured_requests)

        await click(browser_session.page, UPDATE_AND_VIEW, settings=settings, logger=logger)
        with suppress(Exception):
            await browser_session.page.wait_for_load_state("domcontentloaded", timeout=settings.navigation_timeout_ms)
        await asyncio.sleep(3)

        candidate = await _pick_request(captured_requests[baseline:], listing_id=listing_id)
        if candidate is None:
            raise RuntimeError("Unable to identify the update request after clicking the update button.")

        template = await _capture_request_template(
            candidate,
            replacements=replacements,
            validation_url=validation_url,
        )
        resolved_template_path = template_path_for_listing(settings, listing_id)
        _write_template_file(resolved_template_path, template)
        if should_use_legacy_template(settings, listing_id):
            _write_template_file(settings.request_template_path, template)
        log_event(
            logger,
            "request_template_discovered",
            status="success",
            component="discovery",
            template_file=str(resolved_template_path),
            endpoint=template["endpoint"],
            method=template["method"],
            listing_id=listing_id,
        )
        return template
    finally:
        await browser_session.close()
        shutdown_logging()


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover the WG-Gesucht update request template.")
    parser.add_argument(
        "--listing-id",
        default=None,
        help="Specific listing ID to capture. Defaults to the first ID in LISTING_IDS.",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Keep the browser open and wait for a manual click on 'Aktualisieren und Ansehen'.",
    )
    args = parser.parse_args()
    template = asyncio.run(discover_update_request(manual=args.manual, listing_id=args.listing_id))
    print(json.dumps(template, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
