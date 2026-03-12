from __future__ import annotations

import asyncio
import logging
import os
import signal

from bot.alerts import AlertManager
from bot.bump_api import BumpFailedError, SessionRefreshRequiredError, bump_listing
from bot.captcha_detector import CaptchaDetectedError
from bot.config import load_settings, prepare_runtime
from bot.logger import configure_logging, log_event, shutdown_logging
from bot.metrics import MetricsStore
from bot.scheduler import next_delay_seconds, sleep_until_next_cycle
from bot.session_manager import SessionManagerError, load_session, refresh_session


def install_signal_handlers(stop_event: asyncio.Event, logger) -> None:
    loop = asyncio.get_running_loop()

    def request_shutdown(signal_name: str) -> None:
        if stop_event.is_set():
            return
        log_event(
            logger,
            "shutdown_requested",
            status="stopping",
            component="main",
            signal=signal_name,
        )
        stop_event.set()

    for current_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(current_signal, request_shutdown, current_signal.name)
        except NotImplementedError:
            signal.signal(current_signal, lambda *_args, name=current_signal.name: request_shutdown(name))


async def _scheduler_heartbeat(metrics: MetricsStore, cycle_number: int) -> None:
    metrics.record_heartbeat(component="scheduler", cycle=cycle_number)
    metrics.write()


async def _sleep_with_control(
    delay_seconds: float,
    *,
    stop_event: asyncio.Event,
    metrics: MetricsStore,
    cycle_number: int,
    heartbeat_interval_seconds: int,
) -> str | None:
    return await sleep_until_next_cycle(
        delay_seconds,
        stop_event=stop_event,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        on_heartbeat=lambda: _scheduler_heartbeat(metrics, cycle_number),
    )


async def _ensure_session(settings, logger, alerts: AlertManager, *, force_refresh: bool = False) -> None:
    try:
        if not force_refresh:
            load_session(settings=settings)
            return
    except (FileNotFoundError, SessionManagerError):
        pass

    try:
        await refresh_session(settings=settings, logger=logger)
        log_event(logger, "session_ready", status="success", component="session", refreshed=True)
    except CaptchaDetectedError:
        raise
    except Exception as exc:
        await alerts.notify_login_failed(reason=str(exc))
        raise SessionManagerError(str(exc)) from exc


async def _bump_with_session_refresh(listing_id: str, *, settings, logger, alerts, metrics: MetricsStore):
    try:
        return await bump_listing(listing_id, settings=settings, logger=logger, metrics=metrics)
    except SessionRefreshRequiredError as exc:
        log_event(
            logger,
            "session_refresh_requested",
            status="retrying",
            component="session",
            listing_id=listing_id,
            reason=str(exc),
        )
        await _ensure_session(settings, logger, alerts, force_refresh=True)
        return await bump_listing(listing_id, settings=settings, logger=logger, metrics=metrics)


def _get_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


async def _run_cycle(
    *,
    settings,
    logger,
    alerts: AlertManager,
    metrics: MetricsStore,
    stop_event: asyncio.Event,
    cycle_number: int,
) -> int | None:
    metrics.record_heartbeat(component="main", cycle=cycle_number)
    metrics.write()

    log_event(
        logger,
        "bot_alive",
        status="alive",
        component="main",
        cycle=cycle_number,
        listing_ids=list(settings.listing_ids),
        dry_run=settings.dry_run,
    )

    pause_seconds: int | None = None
    await _ensure_session(settings, logger, alerts)
    for listing_id in settings.listing_ids:
        if stop_event.is_set():
            break

        try:
            outcome = await _bump_with_session_refresh(
                listing_id,
                settings=settings,
                logger=logger,
                alerts=alerts,
                metrics=metrics,
            )
        except CaptchaDetectedError as exc:
            metrics.record_failure()
            metrics.write()
            pause_seconds = settings.captcha_retry_delay_seconds
            log_event(
                logger,
                "captcha_detected",
                status="paused",
                component="main",
                level=logging.ERROR,
                listing_id=listing_id,
                error=str(exc),
                retry_in_seconds=pause_seconds,
            )
            await alerts.notify_captcha_detected(details=str(exc))
            break
        except SessionManagerError as exc:
            metrics.record_failure()
            metrics.write()
            pause_seconds = settings.failure_delay_seconds
            log_event(
                logger,
                "login_failed",
                status="error",
                component="main",
                level=logging.ERROR,
                listing_id=listing_id,
                error=str(exc),
                retry_in_seconds=pause_seconds,
            )
            await alerts.notify_login_failed(reason=str(exc))
            break
        except BumpFailedError as exc:
            metrics.record_failure()
            metrics.write()
            log_event(
                logger,
                "listing_update_failed",
                status="error",
                component="main",
                level=logging.ERROR,
                listing_id=listing_id,
                error=str(exc),
            )
            await alerts.notify_listing_update_failed(failed_targets=listing_id)
            continue
        except Exception as exc:
            metrics.record_failure()
            metrics.write()
            log_event(
                logger,
                "listing_update_failed",
                status="error",
                component="main",
                level=logging.ERROR,
                listing_id=listing_id,
                error=str(exc),
            )
            await alerts.notify_listing_update_failed(failed_targets=listing_id)
            continue

        metrics.record_success(outcome.latency_ms)
        metrics.write()

    return pause_seconds


async def _run(single_cycle: bool) -> None:
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
    stop_event = asyncio.Event()
    install_signal_handlers(stop_event, logger)
    cycle_number = 0
    metrics.write()

    try:
        while not stop_event.is_set():
            cycle_number += 1
            try:
                pause_seconds = await _run_cycle(
                    settings=settings,
                    logger=logger,
                    alerts=alerts,
                    metrics=metrics,
                    stop_event=stop_event,
                    cycle_number=cycle_number,
                )

                if stop_event.is_set() or single_cycle:
                    if single_cycle:
                        log_event(
                            logger,
                            "single_run_completed",
                            status="success",
                            component="main",
                            cycle=cycle_number,
                        )
                    break

                delay_seconds = (
                    pause_seconds
                    if pause_seconds is not None
                    else next_delay_seconds(settings.min_delay, settings.max_delay)
                )
                log_event(
                    logger,
                    "next_cycle_scheduled",
                    status="alive",
                    component="scheduler",
                    cycle=cycle_number,
                    next_run_in_seconds=round(delay_seconds, 2),
                )
                outcome = await _sleep_with_control(
                    delay_seconds,
                    stop_event=stop_event,
                    metrics=metrics,
                    cycle_number=cycle_number,
                    heartbeat_interval_seconds=settings.scheduler_heartbeat_interval_seconds,
                )
                if outcome == "stop":
                    break
            finally:
                metrics.write()
    finally:
        metrics.write()
        log_event(logger, "shutdown_complete", status="stopped", component="main")
        shutdown_logging()


async def run_forever() -> None:
    await _run(single_cycle=False)


async def run_once() -> None:
    await _run(single_cycle=True)


def main() -> None:
    asyncio.run(run_once() if _get_bool("RUN_ONCE") else run_forever())


if __name__ == "__main__":
    main()
