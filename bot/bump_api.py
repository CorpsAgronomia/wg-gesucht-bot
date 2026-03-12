from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot.captcha_detector import CaptchaDetectedError
from bot.config import Settings, load_settings
from bot.logger import log_event
from bot.metrics import MetricsStore
from bot.request_client import RequestClient
from bot.request_templates import resolve_template_path
from bot.response_validator import capture_listing_timestamp, validate_response
from bot.session_manager import SessionManagerError, load_session

REQUIRED_TEMPLATE_FIELDS = {
    "endpoint",
    "method",
    "headers",
    "body_template",
    "required_cookies",
    "csrf_field",
}


class BumpFailedError(RuntimeError):
    pass


class SessionRefreshRequiredError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class BumpOutcome:
    listing_id: str
    success: bool
    attempts: int
    status_code: int | None
    latency_ms: float | None
    reason: str
    dry_run: bool


def load_request_template(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid request template: {path}")
    missing = sorted(REQUIRED_TEMPLATE_FIELDS - payload.keys())
    if missing:
        raise ValueError(f"Request template {path} is missing fields: {', '.join(missing)}")
    return payload


async def bump_listing(
    listing_id: str,
    *,
    settings: Settings | None = None,
    logger=None,
    metrics: MetricsStore | None = None,
) -> BumpOutcome:
    settings = settings or load_settings()
    logger = logger or logging.getLogger("wg_bump_bot")
    template_path = resolve_template_path(settings, listing_id)

    session = load_session(settings=settings)
    template = load_request_template(template_path)
    client = RequestClient(settings=settings, logger=logger)
    max_attempts = min(settings.retry_attempts, 5)
    timestamp_before = None if settings.dry_run else await capture_listing_timestamp(client, template, session, listing_id)
    last_reason = "Request validation did not complete."
    last_status_code: int | None = None
    last_latency: float | None = None

    for attempt in range(1, max_attempts + 1):
        execution = await client.send(
            template,
            session,
            listing_id=listing_id,
            dry_run=settings.dry_run,
        )
        validation = await validate_response(
            execution,
            client=client,
            template=template,
            session=session,
            listing_id=listing_id,
            timestamp_before=timestamp_before,
        )
        last_reason = validation.reason
        last_status_code = validation.status_code
        last_latency = execution.latency_ms

        if validation.captcha_detected:
            raise CaptchaDetectedError(validation.reason)

        if validation.session_refresh_required:
            raise SessionRefreshRequiredError(validation.reason)

        if validation.success:
            log_event(
                logger,
                "listing_update_succeeded",
                status="success",
                component="bump_api",
                listing_id=listing_id,
                attempts=attempt,
                status_code=validation.status_code,
                latency_ms=execution.latency_ms,
                dry_run=settings.dry_run,
            )
            return BumpOutcome(
                listing_id=listing_id,
                success=True,
                attempts=attempt,
                status_code=validation.status_code,
                latency_ms=execution.latency_ms,
                reason=validation.reason,
                dry_run=settings.dry_run,
            )

        if attempt < max_attempts:
            if metrics is not None:
                metrics.increment_retries()
            backoff_seconds = min(
                settings.retry_backoff_max_seconds,
                settings.retry_backoff_min_seconds * (settings.retry_backoff_multiplier ** (attempt - 1)),
            )
            log_event(
                logger,
                "listing_update_retrying",
                status="retrying",
                component="bump_api",
                level=logging.WARNING,
                listing_id=listing_id,
                attempt=attempt,
                backoff_seconds=backoff_seconds,
                reason=validation.reason,
            )
            await asyncio.sleep(backoff_seconds)

    log_event(
        logger,
        "listing_update_failed",
        status="error",
        component="bump_api",
        level=logging.ERROR,
        listing_id=listing_id,
        attempts=max_attempts,
        reason=last_reason,
        status_code=last_status_code,
    )
    raise BumpFailedError(last_reason)
