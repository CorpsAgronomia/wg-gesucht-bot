from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress

from bot.logger import log_event


class Watchdog:
    def __init__(self, *, settings, logger, alerts, restart_event: asyncio.Event) -> None:
        self.settings = settings
        self.logger = logger
        self.alerts = alerts
        self.restart_event = restart_event
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        now = time.monotonic()
        self._last_seen = {
            "main": now,
            "browser": now,
            "scheduler": now,
        }
        self._details: dict[str, dict[str, object]] = {}
        self._restart_reason: dict[str, object] | None = None

    @property
    def restart_reason(self) -> dict[str, object] | None:
        return self._restart_reason

    def mark(self, component: str, **details) -> None:
        self._last_seen[component] = time.monotonic()
        if details:
            self._details[component] = details

    async def start(self) -> None:
        self._stop_event.clear()
        self._task = asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    def request_restart(self, component: str, reason: str, **details) -> None:
        if self.restart_event.is_set():
            return

        self._restart_reason = {
            "component": component,
            "reason": reason,
            "details": details or self._details.get(component, {}),
        }
        self.restart_event.set()
        log_event(
            self.logger,
            "watchdog_restart_requested",
            status="restarting",
            component="watchdog",
            level=logging.ERROR,
            watched_component=component,
            reason=reason,
            details=details or self._details.get(component, {}),
        )
        if self.alerts is not None:
            asyncio.create_task(
                self.alerts.notify_watchdog_restart(
                    component=component,
                    details=f"{reason} | {details or self._details.get(component, {})}",
                )
            )

    async def _monitor_loop(self) -> None:
        thresholds = {
            "main": self.settings.watchdog_main_timeout_seconds,
            "browser": self.settings.watchdog_browser_timeout_seconds,
            "scheduler": self.settings.watchdog_scheduler_timeout_seconds,
        }

        while not self._stop_event.is_set():
            now = time.monotonic()
            for component, threshold in thresholds.items():
                age_seconds = now - self._last_seen.get(component, now)
                if age_seconds > threshold:
                    self.request_restart(
                        component,
                        "component_stale",
                        age_seconds=round(age_seconds, 2),
                        threshold_seconds=threshold,
                    )
                    return

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.settings.watchdog_poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                continue
