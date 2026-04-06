from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.async_api import Browser, BrowserContext, Dialog, Page, Playwright, async_playwright

from bot.logger import log_event
from bot.playwright_launcher import launch_chromium_browser
from bot.retry import build_async_retry

if TYPE_CHECKING:
    from bot.alerts import AlertManager
    from bot.config import Settings
    from bot.metrics import MetricsStore
    from bot.watchdog import Watchdog


class BrowserManager:
    def __init__(
        self,
        *,
        settings: Settings,
        logger,
        alerts: AlertManager | None = None,
        metrics: MetricsStore | None = None,
        watchdog: Watchdog | None = None,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.alerts = alerts
        self.metrics = metrics
        self.watchdog = watchdog
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._page_crashed = False
        self._browser_disconnected = False

    @property
    def is_healthy(self) -> bool:
        return (
            self._playwright is not None
            and self._browser is not None
            and self._browser.is_connected()
            and not self._browser_disconnected
            and self._page is not None
            and not self._page.is_closed()
            and not self._page_crashed
        )

    async def start(self, *, load_session: bool = True) -> Page:
        if self._playwright is None:
            self._playwright = await async_playwright().start()

        if self._browser is None or not self._browser.is_connected() or self._browser_disconnected:
            self._browser = await launch_chromium_browser(
                self._playwright,
                headless=self.settings.headless,
                slow_mo_ms=self.settings.slow_mo_ms,
                args=["--disable-dev-shm-usage"],
                logger=self.logger,
                component="browser",
            )
            self._browser.on("disconnected", self._on_browser_disconnected)
            self._browser_disconnected = False
            self._touch_browser(state="started")
            log_event(
                self.logger,
                "browser_started",
                status="success",
                component="browser",
                headless=self.settings.headless,
            )

        await self._create_context(load_session=load_session)
        return self._page

    async def _create_context(self, *, load_session: bool) -> None:
        if self._context is not None:
            await self._close_context()

        storage_state = None
        if load_session and self.settings.storage_state_path.exists():
            storage_state = str(self.settings.storage_state_path)

        self._context = await self._browser.new_context(
            user_agent=self.settings.user_agent,
            viewport={
                "width": self.settings.viewport_width,
                "height": self.settings.viewport_height,
            },
            locale=self.settings.locale,
            timezone_id=self.settings.timezone,
            storage_state=storage_state,
        )
        self._context.set_default_timeout(self.settings.action_timeout_ms)
        self._context.set_default_navigation_timeout(self.settings.navigation_timeout_ms)

        self._page = await self._context.new_page()
        self._page_crashed = False
        self._page.on("crash", lambda: self._on_page_crash())
        self._page.on("dialog", lambda dialog: asyncio.create_task(self._handle_dialog(dialog)))
        self._touch_browser(state="context_ready")

    def _touch_browser(self, **details) -> None:
        if self.watchdog is not None:
            self.watchdog.mark("browser", **details)

    def _spawn_task(self, coroutine) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(coroutine)

    def _on_browser_disconnected(self) -> None:
        self._browser_disconnected = True
        self._touch_browser(state="disconnected")
        log_event(
            self.logger,
            "browser_disconnected",
            status="error",
            component="browser",
            level=logging.ERROR,
        )
        if self.metrics is not None:
            self.metrics.increment_browser_crashes()
            self.metrics.write()
        if self.alerts is not None:
            self._spawn_task(self.alerts.notify_browser_crash(reason="Browser disconnected"))

    def _on_page_crash(self) -> None:
        self._page_crashed = True
        self._touch_browser(state="page_crashed")
        log_event(
            self.logger,
            "page_crashed",
            status="error",
            component="browser",
            level=logging.ERROR,
        )
        if self.metrics is not None:
            self.metrics.increment_browser_crashes()
            self.metrics.write()
        if self.alerts is not None:
            self._spawn_task(self.alerts.notify_browser_crash(reason="Page crashed"))

    async def _handle_dialog(self, dialog: Dialog) -> None:
        try:
            log_event(
                self.logger,
                "dialog_detected",
                status="handling",
                component="browser",
                dialog_type=dialog.type,
                dialog_message=dialog.message[:200],
            )
            await dialog.accept()
        except Exception as exc:
            log_event(
                self.logger,
                "dialog_handling_failed",
                status="error",
                component="browser",
                level=logging.ERROR,
                error=str(exc),
            )

    async def ensure_page(self, *, load_session: bool = True) -> Page:
        if not self.is_healthy:
            return await self.restart(load_session=load_session)

        self._touch_browser(state="ready")
        return self._page

    async def restart(self, *, load_session: bool = True) -> Page:
        await self.stop()
        page = await self.start(load_session=load_session)
        log_event(
            self.logger,
            "browser_restarted",
            status="success",
            component="browser",
            load_session=load_session,
        )
        return page

    async def goto(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        load_session: bool = True,
    ) -> Page:
        retrying = build_async_retry(self.settings)

        async for attempt in retrying:
            with attempt:
                try:
                    page = await self.ensure_page(load_session=load_session)
                    response = await page.goto(
                        url,
                        wait_until=wait_until,
                        timeout=self.settings.navigation_timeout_ms,
                    )
                    if response is not None and response.status >= 500:
                        raise RuntimeError(f"Navigation to {url} returned HTTP {response.status}.")
                    await page.locator("body").wait_for(
                        state="attached",
                        timeout=self.settings.action_timeout_ms,
                    )
                    self._touch_browser(state="navigated", url=url)
                    return page
                except Exception as exc:
                    screenshot = await self.capture_screenshot("navigation_failure")
                    log_event(
                        self.logger,
                        "navigation_failed",
                        status="retrying",
                        component="browser",
                        level=logging.ERROR,
                        error=str(exc),
                        attempt=attempt.retry_state.attempt_number,
                        url=url,
                        screenshot=str(screenshot) if screenshot else None,
                    )
                    await self.restart(load_session=load_session)
                    raise

        raise RuntimeError(f"Failed to navigate to {url}.")

    async def save_session(self) -> Path:
        if self._context is None:
            await self.ensure_page(load_session=False)

        self.settings.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        await self._context.storage_state(path=str(self.settings.storage_state_path))
        self._touch_browser(state="session_saved")
        return self.settings.storage_state_path

    def clear_session(self) -> None:
        if self.settings.storage_state_path.exists():
            self.settings.storage_state_path.unlink()
            self._touch_browser(state="session_cleared")

    async def capture_screenshot(self, prefix: str) -> Path | None:
        self.settings.screenshots_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.settings.screenshots_dir / f"{prefix}_{timestamp}.png"

        try:
            if self._page is None or self._page.is_closed():
                return None
            await self._page.screenshot(path=str(path), full_page=True)
            return path
        except Exception as exc:
            log_event(
                self.logger,
                "screenshot_failed",
                status="error",
                component="browser",
                level=logging.ERROR,
                error=str(exc),
                prefix=prefix,
            )
            return None

    async def stop(self) -> None:
        await self._close_context()

        if self._browser is not None:
            with suppress(Exception):
                await self._browser.close()
            self._browser = None
            self._browser_disconnected = False

        if self._playwright is not None:
            with suppress(Exception):
                await self._playwright.stop()
            self._playwright = None

        self._touch_browser(state="stopped")

    async def _close_context(self) -> None:
        if self._page is not None:
            with suppress(Exception):
                if not self._page.is_closed():
                    await self._page.close()
            self._page = None
            self._page_crashed = False

        if self._context is not None:
            with suppress(Exception):
                await self._context.close()
            self._context = None
