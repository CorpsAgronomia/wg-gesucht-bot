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

    def record_success(self, _latency_ms) -> None:
        return None

    def record_failure(self) -> None:
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

    async def test_forced_refresh_prefers_api_login(self) -> None:
        settings = SimpleNamespace(refresh_session_on_start=True)
        alerts = SimpleNamespace(notify_login_failed=AsyncMock())
        logger = logging.getLogger("test")
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
            patch("main.refresh_session_via_api", new=AsyncMock()) as refresh_via_api,
            patch("main.refresh_session", new=AsyncMock()) as refresh_session,
        ):
            await main._ensure_session(settings, logger, alerts, force_refresh=True)

        refresh_via_api.assert_not_awaited()
        refresh_session.assert_awaited_once_with(
            settings=settings,
            logger=logger,
            prefer_login=True,
        )


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


class UpdateStrategyTest(unittest.IsolatedAsyncioTestCase):
    async def test_update_listing_uses_browser_strategy_by_default(self) -> None:
        settings = SimpleNamespace()
        logger = logging.getLogger("test")
        alerts = SimpleNamespace()
        metrics = _FakeMetrics()
        browser_outcome = SimpleNamespace(latency_ms=123.0)

        with (
            patch("main.bump_listing_via_browser", new=AsyncMock(return_value=browser_outcome)) as bump_browser,
            patch("main.bump_listing", new=AsyncMock()) as bump_request,
        ):
            result = await main._update_listing(
                "12188101",
                settings=settings,
                logger=logger,
                alerts=alerts,
                metrics=metrics,
            )

        self.assertIs(result, browser_outcome)
        bump_browser.assert_awaited_once_with("12188101", settings=settings, logger=logger)
        bump_request.assert_not_awaited()

    async def test_run_cycle_skips_request_session_bootstrap_in_browser_mode(self) -> None:
        settings = SimpleNamespace(
            listing_ids=("12188101",),
            dry_run=False,
            update_strategy="browser",
            captcha_retry_delay_seconds=1800,
            failure_delay_seconds=300,
        )
        logger = logging.getLogger("test")
        alerts = SimpleNamespace(
            notify_captcha_detected=AsyncMock(),
            notify_login_failed=AsyncMock(),
            notify_listing_update_failed=AsyncMock(),
        )
        metrics = _FakeMetrics()
        bump_outcome = SimpleNamespace(latency_ms=42.0)
        stop_event = unittest.mock.Mock()
        stop_event.is_set.return_value = False

        with (
            patch("main._ensure_session", new=AsyncMock()) as ensure_session,
            patch("main._update_listing", new=AsyncMock(return_value=bump_outcome)) as update_listing,
        ):
            outcome = await main._run_cycle(
                settings=settings,
                logger=logger,
                alerts=alerts,
                metrics=metrics,
                stop_event=stop_event,
                cycle_number=1,
            )

        self.assertEqual(outcome.successful_listing_ids, ["12188101"])
        ensure_session.assert_not_awaited()
        update_listing.assert_awaited_once_with(
            "12188101",
            settings=settings,
            logger=logger,
            alerts=alerts,
            metrics=metrics,
        )


if __name__ == "__main__":
    unittest.main()
