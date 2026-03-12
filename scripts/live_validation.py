from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.alerts import AlertManager
from bot.bump_api import BumpFailedError, SessionRefreshRequiredError, bump_listing
from bot.captcha_detector import CaptchaDetectedError
from bot.config import load_settings, prepare_runtime
from bot.logger import configure_logging, log_event, shutdown_logging
from bot.metrics import MetricsStore
from bot.session_manager import SessionManagerError, load_session, refresh_session

VALIDATION_SUCCESS_THRESHOLD = 10


async def _ensure_session(settings, logger, alerts: AlertManager) -> None:
    try:
        load_session(settings=settings)
        return
    except (FileNotFoundError, SessionManagerError):
        pass

    try:
        await refresh_session(settings=settings, logger=logger)
    except Exception as exc:
        await alerts.notify_login_failed(reason=str(exc))
        raise


async def _run_validation_cycle(listing_id: str, *, settings, logger, alerts, metrics: MetricsStore):
    try:
        return await bump_listing(listing_id, settings=settings, logger=logger, metrics=metrics)
    except SessionRefreshRequiredError:
        await refresh_session(settings=settings, logger=logger)
        return await bump_listing(listing_id, settings=settings, logger=logger, metrics=metrics)


async def main_async() -> None:
    settings = load_settings()
    prepare_runtime(settings)
    logger = configure_logging(settings.logs_dir)
    alerts = AlertManager(settings=settings, logger=logger)
    metrics = MetricsStore(
        path=settings.metrics_path,
        logger=logger,
        bot_name=settings.bot_name,
        host_identifier=settings.host_identifier,
    )
    listing_id = settings.listing_ids[0]
    report: list[dict[str, object]] = []
    successful_cycles = 0

    try:
        await _ensure_session(settings, logger, alerts)

        for cycle in range(1, settings.validation_cycles + 1):
            metrics.record_heartbeat(component="validation", cycle=cycle)
            metrics.write()

            status = "failure"
            response_code = None
            latency_ms = None
            reason = ""

            try:
                outcome = await _run_validation_cycle(
                    listing_id,
                    settings=settings,
                    logger=logger,
                    alerts=alerts,
                    metrics=metrics,
                )
                status = "success" if outcome.success else "failure"
                response_code = outcome.status_code
                latency_ms = round(outcome.latency_ms or 0, 2) if outcome.latency_ms is not None else None
                reason = outcome.reason
                if outcome.success:
                    successful_cycles += 1
                    metrics.record_success(outcome.latency_ms)
                else:
                    metrics.record_failure()
            except CaptchaDetectedError as exc:
                reason = str(exc)
                metrics.record_failure()
                await alerts.notify_captcha_detected(details=reason)
            except (BumpFailedError, SessionManagerError, RuntimeError) as exc:
                reason = str(exc)
                metrics.record_failure()
                await alerts.notify_listing_update_failed(failed_targets=listing_id)
            except Exception as exc:
                reason = str(exc)
                metrics.record_failure()
                await alerts.notify_listing_update_failed(failed_targets=listing_id)
                log_event(
                    logger,
                    "validation_cycle_failed",
                    status="error",
                    component="validation",
                    level=logging.ERROR,
                    cycle=cycle,
                    listing_id=listing_id,
                    error=reason,
                )

            entry = {
                "cycle": cycle,
                "status": status,
                "response_code": response_code,
                "latency_ms": latency_ms,
                "reason": reason,
            }
            report.append(entry)
            settings.validation_report_path.write_text(
                json.dumps(report, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
            metrics.write()

            if cycle < settings.validation_cycles:
                await asyncio.sleep(settings.validation_sleep_seconds)

        metrics_payload = json.loads(settings.metrics_path.read_text(encoding="utf-8"))
        metrics_written = all(
            key in metrics_payload
            for key in ("successful_updates", "failed_updates", "retries", "response_times")
        )
        if (
            settings.validation_cycles >= VALIDATION_SUCCESS_THRESHOLD
            and successful_cycles == settings.validation_cycles
            and metrics_written
        ):
            print("SYSTEM FULLY VALIDATED")
    finally:
        metrics.write()
        shutdown_logging()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
