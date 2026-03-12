from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from bot.request_client import RequestClient, RequestExecution
from bot.session_manager import SessionData

CAPTCHA_MARKERS = ("captcha", "verify you are human", "robot check")
FAILURE_MARKERS = ("fehlgeschlagen", "error", "forbidden", "unauthorized")
SUCCESS_MARKERS = ("erfolgreich", "aktualisiert", "success", "updated")
LAST_UPDATED_HTML_PATTERNS = (
    re.compile(r'class=["\'][^"\']*last_updated_date[^"\']*["\'][^>]*>\s*([^<]{4,64})\s*<', re.IGNORECASE),
    re.compile(
        r"Zuletzt\s+aktualisiert:\s*(?:<[^>]+>\s*)?([^<\n]{4,64})",
        re.IGNORECASE,
    ),
)
TIMESTAMP_PATTERNS = (
    re.compile(
        r"(?:Zuletzt\s+aktualisiert|Aktualisiert|Stand|Letzte\s+Anderung|Letzte\s+Änderung)"
        r"\s*[:\-]?\s*([^\n<]{4,64})",
        re.IGNORECASE,
    ),
    re.compile(r"(\d{2}\.\d{2}\.\d{4}\s*-\s*\d{2}:\d{2})"),
    re.compile(r"((?:Heute|Gestern|\d{1,2}\.\d{1,2}\.\d{2,4})\s*,?\s*\d{1,2}:\d{2}\s*Uhr)", re.IGNORECASE),
)


@dataclass(slots=True, frozen=True)
class ValidationResult:
    success: bool
    reason: str
    status_code: int | None
    success_indicator: bool
    timestamp_before: str | None
    timestamp_after: str | None
    captcha_detected: bool
    session_refresh_required: bool


def _extract_json_payload(response) -> Any | None:
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError):
        return None


def _stringify_success(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"ok", "success", "true", "updated"}
    return False


def _has_success_indicator(response) -> bool:
    payload = _extract_json_payload(response)
    if isinstance(payload, dict):
        for key in ("success", "ok", "status", "result"):
            if key in payload and _stringify_success(payload[key]):
                return True
    text = response.text.lower()
    return any(marker in text for marker in SUCCESS_MARKERS)


def _extract_timestamp(text: str) -> str | None:
    for pattern in LAST_UPDATED_HTML_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1).strip()

    cleaned = re.sub(r"\s+", " ", text)
    for pattern in TIMESTAMP_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            return match.group(1).strip()
    return None


async def capture_listing_timestamp(
    client: RequestClient,
    template: dict[str, Any],
    session: SessionData,
    listing_id: str,
) -> str | None:
    validation_url = str(
        template.get("validation_url_template")
        or template.get("headers", {}).get("referer")
        or template.get("headers", {}).get("Referer")
        or ""
    ).strip()
    if not validation_url:
        return None

    validation_url = (
        validation_url.replace("{listing_id}", listing_id)
        .replace("{csrf_token}", session.csrf_token)
        .replace("{csrf}", session.csrf_token)
    )
    response = await client.get(validation_url, session, headers={"Referer": validation_url})
    if response.status_code >= 400:
        return None
    return _extract_timestamp(response.text)


async def validate_response(
    execution: RequestExecution,
    *,
    client: RequestClient,
    template: dict[str, Any],
    session: SessionData,
    listing_id: str,
    timestamp_before: str | None,
) -> ValidationResult:
    if execution.dry_run:
        return ValidationResult(
            success=True,
            reason="DRY_RUN enabled; request was constructed but not sent.",
            status_code=None,
            success_indicator=True,
            timestamp_before=timestamp_before,
            timestamp_after=timestamp_before,
            captcha_detected=False,
            session_refresh_required=False,
        )

    response = execution.response
    assert response is not None

    text = response.text.lower()
    captcha_detected = any(marker in text or marker in str(response.url).lower() for marker in CAPTCHA_MARKERS)
    session_refresh_required = response.status_code in {401, 403} or (
        "login" in str(response.url).lower() and "passwort" in text
    )
    success_indicator = _has_success_indicator(response)
    timestamp_after = await capture_listing_timestamp(client, template, session, listing_id)
    timestamp_updated = (
        timestamp_before is not None
        and timestamp_after is not None
        and timestamp_before != timestamp_after
    )
    body_failed = any(marker in text for marker in FAILURE_MARKERS)
    status_ok = response.status_code == 200

    if captcha_detected:
        return ValidationResult(
            success=False,
            reason="CAPTCHA detected in response payload.",
            status_code=response.status_code,
            success_indicator=success_indicator,
            timestamp_before=timestamp_before,
            timestamp_after=timestamp_after,
            captcha_detected=True,
            session_refresh_required=False,
        )

    if session_refresh_required:
        return ValidationResult(
            success=False,
            reason="Session appears to be invalid or expired.",
            status_code=response.status_code,
            success_indicator=success_indicator,
            timestamp_before=timestamp_before,
            timestamp_after=timestamp_after,
            captcha_detected=False,
            session_refresh_required=True,
        )

    success = status_ok and not body_failed and (success_indicator or timestamp_updated or timestamp_after is None)
    reason = "Request validated successfully." if success else (
        "Response did not satisfy validation checks "
        f"(status_code={response.status_code}, success_indicator={success_indicator}, "
        f"timestamp_before={timestamp_before!r}, timestamp_after={timestamp_after!r})."
    )
    return ValidationResult(
        success=success,
        reason=reason,
        status_code=response.status_code,
        success_indicator=success_indicator,
        timestamp_before=timestamp_before,
        timestamp_after=timestamp_after,
        captcha_detected=False,
        session_refresh_required=False,
    )
