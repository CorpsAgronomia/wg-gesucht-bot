from __future__ import annotations

import asyncio
import json
import time
from urllib import parse, request

from bot.logger import log_event


class AlertManager:
    def __init__(self, *, settings, logger) -> None:
        self.settings = settings
        self.logger = logger
        self._last_sent_at: dict[str, float] = {}
        self._enabled = bool(settings.telegram_bot_token and settings.telegram_chat_id)

        if not self._enabled:
            log_event(
                self.logger,
                "telegram_alerts_disabled",
                status="disabled",
                component="alerts",
            )

    async def send(self, key: str, message: str) -> bool:
        if not self._enabled:
            return False

        now = time.monotonic()
        previous = self._last_sent_at.get(key)
        if previous is not None and (now - previous) < self.settings.alert_cooldown_seconds:
            log_event(
                self.logger,
                "telegram_alert_suppressed",
                status="suppressed",
                component="alerts",
                alert_key=key,
            )
            return False

        self._last_sent_at[key] = now
        payload = parse.urlencode(
            {
                "chat_id": self.settings.telegram_chat_id,
                "text": message,
                "disable_web_page_preview": "true",
            }
        ).encode()

        try:
            await asyncio.to_thread(self._post_telegram_message, payload)
            log_event(
                self.logger,
                "telegram_alert_sent",
                status="success",
                component="alerts",
                alert_key=key,
            )
            return True
        except Exception as exc:
            log_event(
                self.logger,
                "telegram_alert_failed",
                status="error",
                component="alerts",
                level=40,
                error=str(exc),
                alert_key=key,
            )
            return False

    def _post_telegram_message(self, payload: bytes) -> None:
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        req = request.Request(url, data=payload, method="POST")
        with request.urlopen(req, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API returned an error: {data}")

    def _prefix(self) -> str:
        return f"[{self.settings.bot_name}@{self.settings.host_identifier}]"

    async def notify_login_failed(self, *, reason: str, screenshot: str | None = None) -> bool:
        message = f"{self._prefix()} Login failed.\nReason: {reason}"
        if screenshot:
            message += f"\nScreenshot: {screenshot}"
        return await self.send("login_failed", message)

    async def notify_captcha_detected(self, *, details: str, screenshot: str | None = None) -> bool:
        message = f"{self._prefix()} CAPTCHA detected.\nDetails: {details}\nRetry: 30 minutes"
        if screenshot:
            message += f"\nScreenshot: {screenshot}"
        return await self.send("captcha_detected", message)

    async def notify_listing_update_failed(
        self,
        *,
        failed_targets: str,
        screenshot: str | None = None,
    ) -> bool:
        message = f"{self._prefix()} Listing update failed.\nTargets: {failed_targets}"
        if screenshot:
            message += f"\nScreenshot: {screenshot}"
        return await self.send("listing_update_failed", message)

    async def notify_browser_crash(self, *, reason: str) -> bool:
        message = f"{self._prefix()} Browser crash detected.\nReason: {reason}"
        return await self.send("browser_crash", message)

    async def notify_watchdog_restart(self, *, component: str, details: str) -> bool:
        message = (
            f"{self._prefix()} Watchdog requested a restart.\n"
            f"Component: {component}\nDetails: {details}"
        )
        return await self.send("watchdog_restart", message)

