from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.playwright_launcher import launch_chromium_browser


class PlaywrightLauncherTest(unittest.IsolatedAsyncioTestCase):
    async def test_uses_bundled_browser_when_available(self) -> None:
        browser = object()
        launch = AsyncMock(return_value=browser)
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))

        result = await launch_chromium_browser(
            playwright,
            headless=True,
            slow_mo_ms=0,
            args=["--disable-dev-shm-usage"],
            logger=logging.getLogger("test"),
            component="session",
        )

        self.assertIs(result, browser)
        launch.assert_awaited_once_with(
            headless=True,
            slow_mo=0,
            args=["--disable-dev-shm-usage"],
        )

    async def test_falls_back_to_installed_chrome_when_bundled_browser_is_missing(self) -> None:
        browser = object()

        async def launch(**kwargs):
            if "channel" not in kwargs:
                raise RuntimeError("Executable doesn't exist")
            if kwargs["channel"] == "chrome":
                return browser
            raise RuntimeError("channel missing")

        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(side_effect=launch)))

        with patch("bot.playwright_launcher.log_event") as log_event:
            result = await launch_chromium_browser(
                playwright,
                headless=True,
                slow_mo_ms=0,
                args=["--disable-dev-shm-usage"],
                logger=logging.getLogger("test"),
                component="session",
            )

        self.assertIs(result, browser)
        log_event.assert_called_once()


if __name__ == "__main__":
    unittest.main()
