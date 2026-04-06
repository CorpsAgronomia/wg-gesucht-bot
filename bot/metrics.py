from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot.logger import log_event


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class MetricsStore:
    path: Path
    logger: object
    bot_name: str
    host_identifier: str
    started_at: str = field(default_factory=_utc_now)
    _started_monotonic: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    successful_updates: int = 0
    failed_updates: int = 0
    retries: int = 0
    browser_crashes: int = 0
    response_times: list[float] = field(default_factory=list)
    cycle: int = 0
    last_heartbeat_at: str | None = None
    last_success_at: str | None = None
    last_error_at: str | None = None
    component_heartbeats: dict[str, str] = field(default_factory=dict)

    def record_heartbeat(self, *, component: str, cycle: int | None = None) -> None:
        timestamp = _utc_now()
        with self._lock:
            self.last_heartbeat_at = timestamp
            self.component_heartbeats[component] = timestamp
            if cycle is not None:
                self.cycle = cycle

    def record_success(self, latency_ms: float | None = None) -> None:
        with self._lock:
            self.successful_updates += 1
            self.last_success_at = _utc_now()
            if latency_ms is not None:
                self.response_times.append(round(latency_ms, 2))

    def record_failure(self) -> None:
        with self._lock:
            self.failed_updates += 1
            self.last_error_at = _utc_now()

    def increment_retries(self, count: int = 1) -> None:
        with self._lock:
            self.retries += count

    def increment_browser_crashes(self, count: int = 1) -> None:
        with self._lock:
            self.browser_crashes += count

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "bot_name": self.bot_name,
                "host_identifier": self.host_identifier,
                "started_at": self.started_at,
                "uptime_seconds": round(time.monotonic() - self._started_monotonic, 2),
                "cycle": self.cycle,
                "successful_updates": self.successful_updates,
                "failed_updates": self.failed_updates,
                "retries": self.retries,
                "browser_crashes": self.browser_crashes,
                "response_times": list(self.response_times),
                "last_heartbeat_at": self.last_heartbeat_at,
                "last_success_at": self.last_success_at,
                "last_error_at": self.last_error_at,
                "component_heartbeats": dict(self.component_heartbeats),
            }

    def write(self) -> None:
        payload = self.snapshot()
        temporary_path = self.path.with_suffix(".tmp")
        try:
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
            temporary_path.replace(self.path)
        except Exception as exc:
            log_event(
                self.logger,
                "metrics_write_failed",
                status="error",
                component="metrics",
                level=40,
                error=str(exc),
                metrics_file=str(self.path),
            )
