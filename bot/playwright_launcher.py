from __future__ import annotations

from typing import Sequence

from bot.logger import log_event

FALLBACK_BROWSER_CHANNELS = ("chrome", "msedge")


def _is_missing_browser_error(exc: Exception) -> bool:
    message = str(exc)
    return "Executable doesn't exist" in message or "Please run the following command to download new browsers" in message


async def launch_chromium_browser(
    playwright,
    *,
    headless: bool,
    slow_mo_ms: int,
    args: Sequence[str],
    logger,
    component: str,
):
    last_error: Exception | None = None
    launch_args = list(args)

    try:
        return await playwright.chromium.launch(
            headless=headless,
            slow_mo=slow_mo_ms,
            args=launch_args,
        )
    except Exception as exc:
        last_error = exc
        if not _is_missing_browser_error(exc):
            raise

    for channel in FALLBACK_BROWSER_CHANNELS:
        try:
            browser = await playwright.chromium.launch(
                channel=channel,
                headless=headless,
                slow_mo=slow_mo_ms,
                args=launch_args,
            )
        except Exception as exc:
            last_error = exc
            continue

        log_event(
            logger,
            "browser_launch_fallback_used",
            status="warning",
            component=component,
            browser_channel=channel,
        )
        return browser

    if last_error is not None:
        raise RuntimeError(
            "Unable to launch a Chromium browser. Tried bundled Playwright Chromium and installed Chrome/Edge channels."
        ) from last_error

    raise RuntimeError("Unable to launch a Chromium browser.")
