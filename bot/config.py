from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

HEADLESS = False
MIN_DELAY = 7200
MAX_DELAY = 14400
RETRY_ATTEMPTS = 5
UPDATE_STRATEGY = "browser"


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(slots=True, frozen=True)
class Settings:
    base_url: str
    email: str
    password: str
    listing_title: str
    listing_ids: tuple[str, ...]
    listing_targets: tuple[str, ...]
    update_strategy: str
    headless: bool
    min_delay: int
    max_delay: int
    retry_attempts: int
    retry_backoff_multiplier: int
    retry_backoff_min_seconds: int
    retry_backoff_max_seconds: int
    selector_retry_attempts: int
    selector_timeout_ms: int
    navigation_timeout_ms: int
    action_timeout_ms: int
    slow_mo_ms: int
    failure_delay_seconds: int
    captcha_retry_delay_seconds: int
    scheduler_heartbeat_interval_seconds: int
    alert_cooldown_seconds: int
    timezone: str
    locale: str
    user_agent: str
    viewport_width: int
    viewport_height: int
    session_file: Path
    storage_state_path: Path
    logs_dir: Path
    screenshots_dir: Path
    metrics_path: Path
    request_template_path: Path
    request_templates_dir: Path
    reports_dir: Path
    validation_report_path: Path
    request_timeout_seconds: int
    dry_run: bool
    refresh_session_on_start: bool
    validation_cycles: int
    validation_sleep_seconds: int
    telegram_bot_token: str
    telegram_chat_id: str
    bot_name: str
    host_identifier: str


def load_settings() -> Settings:
    listing_title = os.getenv("LISTING_TITLE", "").strip()
    listing_ids = _split_csv(os.getenv("LISTING_IDS", ""))
    listing_targets = tuple(dict.fromkeys([*listing_ids, *([listing_title] if listing_title else [])]))
    session_file = Path(os.getenv("SESSION_FILE", "auth/session.json"))

    settings = Settings(
        base_url=os.getenv("WG_BASE_URL", "https://www.wg-gesucht.de/").strip(),
        email=os.getenv("WG_EMAIL", "").strip(),
        password=os.getenv("WG_PASSWORD", "").strip(),
        listing_title=listing_title,
        listing_ids=listing_ids,
        listing_targets=listing_targets,
        update_strategy=(os.getenv("UPDATE_STRATEGY", UPDATE_STRATEGY).strip().lower() or UPDATE_STRATEGY),
        headless=_get_bool("HEADLESS", HEADLESS),
        min_delay=int(os.getenv("MIN_DELAY", str(MIN_DELAY))),
        max_delay=int(os.getenv("MAX_DELAY", str(MAX_DELAY))),
        retry_attempts=int(os.getenv("RETRY_ATTEMPTS", str(RETRY_ATTEMPTS))),
        retry_backoff_multiplier=int(os.getenv("RETRY_BACKOFF_MULTIPLIER", "2")),
        retry_backoff_min_seconds=int(os.getenv("RETRY_BACKOFF_MIN_SECONDS", "2")),
        retry_backoff_max_seconds=int(os.getenv("RETRY_BACKOFF_MAX_SECONDS", "60")),
        selector_retry_attempts=int(os.getenv("SELECTOR_RETRY_ATTEMPTS", "3")),
        selector_timeout_ms=int(os.getenv("SELECTOR_TIMEOUT_MS", "8000")),
        navigation_timeout_ms=int(os.getenv("NAVIGATION_TIMEOUT_MS", "60000")),
        action_timeout_ms=int(os.getenv("ACTION_TIMEOUT_MS", "30000")),
        slow_mo_ms=int(os.getenv("SLOW_MO_MS", "0")),
        failure_delay_seconds=int(os.getenv("FAILURE_DELAY_SECONDS", "300")),
        captcha_retry_delay_seconds=int(os.getenv("CAPTCHA_RETRY_DELAY_SECONDS", "1800")),
        scheduler_heartbeat_interval_seconds=int(os.getenv("SCHEDULER_HEARTBEAT_INTERVAL_SECONDS", "60")),
        alert_cooldown_seconds=int(os.getenv("ALERT_COOLDOWN_SECONDS", "900")),
        timezone=os.getenv("TIMEZONE", "Europe/Berlin"),
        locale=os.getenv("LOCALE", "de-DE"),
        user_agent=os.getenv(
            "USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
        ),
        viewport_width=int(os.getenv("VIEWPORT_WIDTH", "1440")),
        viewport_height=int(os.getenv("VIEWPORT_HEIGHT", "960")),
        session_file=session_file,
        storage_state_path=session_file,
        logs_dir=Path(os.getenv("LOGS_DIR", "logs")),
        screenshots_dir=Path(os.getenv("SCREENSHOTS_DIR", "logs/screenshots")),
        metrics_path=Path(os.getenv("METRICS_FILE", "logs/metrics.json")),
        request_template_path=Path(
            os.getenv("UPDATE_REQUEST_TEMPLATE_FILE", "discovery/update_request_template.json")
        ),
        request_templates_dir=Path(
            os.getenv("UPDATE_REQUEST_TEMPLATES_DIR", "discovery/update_request_templates")
        ),
        reports_dir=Path(os.getenv("REPORTS_DIR", "reports")),
        validation_report_path=Path(
            os.getenv("VALIDATION_REPORT_FILE", "reports/validation_report.json")
        ),
        request_timeout_seconds=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "10")),
        dry_run=_get_bool("DRY_RUN", True),
        refresh_session_on_start=_get_bool("REFRESH_SESSION_ON_START", False),
        validation_cycles=int(os.getenv("VALIDATION_CYCLES", "10")),
        validation_sleep_seconds=int(os.getenv("VALIDATION_SLEEP_SECONDS", "60")),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        bot_name=os.getenv("BOT_NAME", "wg-bump-bot").strip() or "wg-bump-bot",
        host_identifier=os.getenv("BOT_HOSTNAME", socket.gethostname()).strip() or socket.gethostname(),
    )

    missing = [
        name
        for name, value in {
            "WG_EMAIL": settings.email,
            "WG_PASSWORD": settings.password,
            "LISTING_IDS": ",".join(settings.listing_ids),
        }.items()
        if not value
    ]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
    if settings.min_delay <= 0 or settings.max_delay <= 0:
        raise ValueError("MIN_DELAY and MAX_DELAY must be positive integers.")
    if settings.min_delay > settings.max_delay:
        raise ValueError("MIN_DELAY must be less than or equal to MAX_DELAY.")
    if settings.retry_attempts <= 0:
        raise ValueError("RETRY_ATTEMPTS must be positive.")
    if settings.update_strategy not in {"browser", "request"}:
        raise ValueError("UPDATE_STRATEGY must be either 'browser' or 'request'.")
    if settings.request_timeout_seconds <= 0:
        raise ValueError("REQUEST_TIMEOUT_SECONDS must be positive.")
    if settings.validation_cycles <= 0:
        raise ValueError("VALIDATION_CYCLES must be positive.")
    if settings.validation_sleep_seconds < 0:
        raise ValueError("VALIDATION_SLEEP_SECONDS must be zero or positive.")

    return settings


def prepare_runtime(settings: Settings) -> None:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    settings.screenshots_dir.mkdir(parents=True, exist_ok=True)
    settings.session_file.parent.mkdir(parents=True, exist_ok=True)
    settings.metrics_path.parent.mkdir(parents=True, exist_ok=True)
    settings.request_template_path.parent.mkdir(parents=True, exist_ok=True)
    settings.request_templates_dir.mkdir(parents=True, exist_ok=True)
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
