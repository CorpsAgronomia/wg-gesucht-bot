from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from bot.bump_listing import _click_with_overlay_retries, _find_confirmation, submit_listing_update


class ClickWithOverlayRetriesTest(unittest.IsolatedAsyncioTestCase):
    async def test_retries_after_overlay_interference(self) -> None:
        page = SimpleNamespace(wait_for_timeout=AsyncMock())
        locator = AsyncMock()
        locator.click = AsyncMock(side_effect=[RuntimeError("blocked"), None])

        with patch("bot.bump_listing._dismiss_blocking_modals", new=AsyncMock()) as dismiss_modals:
            await _click_with_overlay_retries(
                locator,
                page,
                logging.getLogger("test"),
                timeout_ms=3000,
            )

        self.assertEqual(locator.click.await_count, 2)
        self.assertEqual(dismiss_modals.await_count, 6)


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
                call(update_button, page, logger, timeout_ms=settings.action_timeout_ms),
                call(confirmation, page, logger, timeout_ms=settings.action_timeout_ms),
            ],
        )
        self.assertEqual(ensure_no_captcha.await_count, 2)
        self.assertGreaterEqual(stabilize.await_count, 3)


if __name__ == "__main__":
    unittest.main()
