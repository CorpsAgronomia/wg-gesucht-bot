from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from bot.bump_listing import (
    ListingUpdateError,
    _accept_blocking_modals,
    _click_with_overlay_retries,
    _find_confirmation,
    bump_listing_via_browser,
    submit_listing_update,
)


class AcceptBlockingModalsTest(unittest.IsolatedAsyncioTestCase):
    async def test_accept_blocking_modals_clicks_consent_controls(self) -> None:
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value=1)
        settings = SimpleNamespace(action_timeout_ms=3000)

        with (
            patch("bot.bump_listing.click_optional", new=AsyncMock(return_value=True)) as click_optional,
            patch("bot.bump_listing.log_event") as log_event,
        ):
            await _accept_blocking_modals(page, settings, logging.getLogger("test"))

        click_optional.assert_awaited_once()
        script = page.evaluate.await_args.args[0]
        self.assertIn("#cmpbox button.cmpboxbtnyes", script)
        self.assertIn("acceptPattern", script)
        log_event.assert_called_once()


class ClickWithOverlayRetriesTest(unittest.IsolatedAsyncioTestCase):
    async def test_retries_after_overlay_interference(self) -> None:
        settings = SimpleNamespace()
        page = SimpleNamespace(wait_for_timeout=AsyncMock())
        locator = AsyncMock()
        locator.click = AsyncMock(side_effect=[RuntimeError("blocked"), None])

        with patch("bot.bump_listing._stabilize_editor_state", new=AsyncMock()) as stabilize:
            await _click_with_overlay_retries(
                locator,
                page,
                settings,
                logging.getLogger("test"),
                timeout_ms=3000,
            )

        self.assertEqual(locator.click.await_count, 2)
        self.assertEqual(stabilize.await_count, 2)

    async def test_force_clicks_after_final_overlay_failure(self) -> None:
        settings = SimpleNamespace()
        page = SimpleNamespace(wait_for_timeout=AsyncMock())
        locator = AsyncMock()
        locator.click = AsyncMock(side_effect=[RuntimeError("blocked"), RuntimeError("blocked"), None])

        with (
            patch("bot.bump_listing._stabilize_editor_state", new=AsyncMock()),
            patch("bot.bump_listing._accept_blocking_modals", new=AsyncMock()) as accept_modals,
        ):
            await _click_with_overlay_retries(
                locator,
                page,
                settings,
                logging.getLogger("test"),
                timeout_ms=3000,
                attempts=2,
            )

        self.assertEqual(
            locator.click.await_args_list,
            [
                call(timeout=3000),
                call(timeout=3000),
                call(timeout=3000, force=True),
            ],
        )
        accept_modals.assert_awaited_once()


class FindConfirmationTest(unittest.IsolatedAsyncioTestCase):
    async def test_prefers_confirmation_inside_visible_dialog(self) -> None:
        settings = SimpleNamespace()
        logger = logging.getLogger("test")
        page = SimpleNamespace(locator=Mock(return_value="dialog-scope"))
        dialog_confirmation = object()

        with (
            patch("bot.bump_listing._stabilize_editor_state", new=AsyncMock()) as stabilize,
            patch(
                "bot.bump_listing.resolve_optional",
                new=AsyncMock(side_effect=[dialog_confirmation]),
            ) as resolve_optional,
        ):
            result = await _find_confirmation(page, settings, logger)

        self.assertIs(result, dialog_confirmation)
        stabilize.assert_awaited_once()
        self.assertEqual(resolve_optional.await_count, 1)
        first_call = resolve_optional.await_args_list[0]
        self.assertEqual(first_call.args[0], "dialog-scope")


class SubmitListingUpdateTest(unittest.IsolatedAsyncioTestCase):
    async def test_submit_listing_update_uses_overlay_safe_clicks(self) -> None:
        logger = logging.getLogger("test")
        settings = SimpleNamespace(
            action_timeout_ms=30000,
            navigation_timeout_ms=1000,
            selector_timeout_ms=1000,
        )
        page = AsyncMock()
        page.url = "https://www.wg-gesucht.de/editor"

        update_button = AsyncMock()
        confirmation = AsyncMock()

        with (
            patch("bot.bump_listing.resolve", new=AsyncMock(return_value=update_button)) as resolve,
            patch("bot.bump_listing._find_confirmation", new=AsyncMock(return_value=confirmation)) as find_confirmation,
            patch("bot.bump_listing._click_with_overlay_retries", new=AsyncMock()) as safe_click,
            patch("bot.bump_listing._stabilize_editor_state", new=AsyncMock()) as stabilize,
            patch("bot.bump_listing.ensure_no_captcha", new=AsyncMock()) as ensure_no_captcha,
            patch("bot.bump_listing.resolve_optional", new=AsyncMock(return_value=None)),
        ):
            await submit_listing_update(page, settings, logger, "12188101")

        resolve.assert_awaited_once()
        find_confirmation.assert_awaited_once_with(page, settings, logger)
        self.assertEqual(
            safe_click.await_args_list,
            [
                call(update_button, page, settings, logger, timeout_ms=settings.action_timeout_ms),
                call(confirmation, page, settings, logger, timeout_ms=settings.action_timeout_ms),
            ],
        )
        self.assertEqual(ensure_no_captcha.await_count, 2)
        self.assertGreaterEqual(stabilize.await_count, 3)


class DismissBlockingModalsTest(unittest.IsolatedAsyncioTestCase):
    async def test_dismiss_blocking_modals_removes_cmpbox_overlays(self) -> None:
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value=4)

        with patch("bot.bump_listing.log_event") as log_event:
            from bot.bump_listing import _dismiss_blocking_modals

            await _dismiss_blocking_modals(page, logging.getLogger("test"))

        script = page.evaluate.await_args.args[0]
        self.assertIn("#cmpbox", script)
        self.assertIn("#cmpbox2", script)
        self.assertIn(".cmpbox", script)
        self.assertIn(".cmpboxBG", script)
        self.assertIn("#private_users_ad_modal", script)
        log_event.assert_called_once()


class BrowserBumpFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_browser_bump_uses_timestamp_fallback_when_no_transition_is_observed(self) -> None:
        settings = SimpleNamespace(
            dry_run=False,
            retry_attempts=1,
            retry_backoff_multiplier=1,
            retry_backoff_min_seconds=1,
            retry_backoff_max_seconds=1,
        )
        browser_session = SimpleNamespace(page=object(), close=AsyncMock())
        verification = object()

        with (
            patch("bot.bump_listing._prepare_timestamp_verification", new=AsyncMock(return_value=verification)),
            patch("bot.bump_listing._open_editor_page_for_listing", new=AsyncMock(return_value=browser_session)),
            patch(
                "bot.bump_listing.submit_listing_update",
                new=AsyncMock(side_effect=ListingUpdateError("No post-update transition was observed for listing '12188101'.")),
            ),
            patch("bot.bump_listing._verify_listing_update_via_timestamp", new=AsyncMock(return_value=True)) as verify,
        ):
            outcome = await bump_listing_via_browser("12188101", settings=settings, logger=logging.getLogger("test"))

        self.assertTrue(outcome.success)
        self.assertEqual(
            outcome.reason,
            "Listing updated through the browser flow and verified via listing timestamp.",
        )
        verify.assert_awaited_once_with(
            "12188101",
            settings=settings,
            logger=logging.getLogger("test"),
            verification=verification,
        )
        browser_session.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
