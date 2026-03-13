from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import main
from bot.session_manager import SessionData, SessionManagerError


class _FakeMetrics:
    def write(self) -> None:
        return None

    def record_heartbeat(self, **_kwargs) -> None:
        return None


class EnsureSessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_startup_refresh_failure_reuses_existing_session(self) -> None:
        settings = SimpleNamespace(refresh_session_on_start=True)
        alerts = SimpleNamespace(notify_login_failed=AsyncMock())
        session = SessionData(
            cookies=[{"name": "sessionid", "value": "abc", "domain": ".wg-gesucht.de", "path": "/"}],
            csrf_token="csrf-token",
            user_agent="UnitTestAgent/1.0",
            captured_at="2026-03-13T00:00:00+00:00",
            access_token="access-token",
            refresh_token="refresh-token",
            client_id="client-id",
            dev_ref_no="dev-ref",
            user_id="12345678",
            login_token="login-token",
        )

        with (
            patch("main.load_session", return_value=session),
            patch(
                "main.refresh_session_via_api",
                new=AsyncMock(side_effect=SessionManagerError("temporary refresh failure")),
            ) as refresh_via_api,
            patch("main.refresh_session", new=AsyncMock()) as refresh_session,
            patch("main.save_session") as save_session,
        ):
            await main._ensure_session(settings, logging.getLogger("test"), alerts)

        refresh_via_api.assert_awaited_once()
        refresh_session.assert_not_awaited()
        save_session.assert_not_called()
        alerts.notify_login_failed.assert_not_awaited()


class RunOnceTest(unittest.IsolatedAsyncioTestCase):
    def _settings(self, temp_dir: str) -> SimpleNamespace:
        temp_path = Path(temp_dir)
        return SimpleNamespace(
            listing_ids=("12188101", "13127492"),
            logs_dir=temp_path / "logs",
            metrics_path=temp_path / "logs" / "metrics.json",
            bot_name="wg-bump-bot",
            host_identifier="unit-test",
            scheduler_heartbeat_interval_seconds=60,
            min_delay=1,
            max_delay=2,
        )

    async def test_run_once_raises_when_any_listing_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._settings(temp_dir)
            cycle_outcome = main.CycleOutcome(
                successful_listing_ids=["12188101"],
                failed_listing_ids=["13127492"],
            )

            with (
                patch("main.load_settings", return_value=settings),
                patch("main.prepare_runtime"),
                patch("main.configure_logging", return_value=logging.getLogger("test")),
                patch("main.AlertManager", return_value=SimpleNamespace()),
                patch("main.MetricsStore", return_value=_FakeMetrics()),
                patch("main.install_signal_handlers"),
                patch("main.shutdown_logging"),
                patch("main._run_cycle", new=AsyncMock(return_value=cycle_outcome)),
            ):
                with self.assertRaisesRegex(RuntimeError, "One or more listing updates failed."):
                    await main.run_once()

    async def test_run_once_succeeds_when_all_listings_updated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._settings(temp_dir)
            cycle_outcome = main.CycleOutcome(
                successful_listing_ids=["12188101", "13127492"],
            )

            with (
                patch("main.load_settings", return_value=settings),
                patch("main.prepare_runtime"),
                patch("main.configure_logging", return_value=logging.getLogger("test")),
                patch("main.AlertManager", return_value=SimpleNamespace()),
                patch("main.MetricsStore", return_value=_FakeMetrics()),
                patch("main.install_signal_handlers"),
                patch("main.shutdown_logging"),
                patch("main._run_cycle", new=AsyncMock(return_value=cycle_outcome)) as run_cycle,
            ):
                await main.run_once()

        run_cycle.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
