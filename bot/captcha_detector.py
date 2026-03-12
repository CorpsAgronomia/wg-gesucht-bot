from __future__ import annotations

from playwright.async_api import Page

CAPTCHA_PATTERNS = (
    "captcha",
    "verify you are human",
    "robot check",
)


class CaptchaDetectedError(RuntimeError):
    pass


async def detect_captcha(page: Page) -> str | None:
    url = page.url.lower()
    title = ""
    body_text = ""
    iframe_match = False

    try:
        title = (await page.title()).lower()
    except Exception:
        title = ""

    try:
        body_text = ((await page.locator("body").text_content(timeout=3000)) or "").lower()
    except Exception:
        body_text = ""

    try:
        iframe_match = await page.locator("iframe[src*='captcha' i], iframe[title*='captcha' i]").count() > 0
    except Exception:
        iframe_match = False

    haystack = " ".join(part for part in (url, title, body_text) if part)
    for pattern in CAPTCHA_PATTERNS:
        if pattern in haystack:
            return pattern

    if iframe_match:
        return "captcha iframe"

    return None


async def ensure_no_captcha(page: Page) -> None:
    match = await detect_captcha(page)
    if match is not None:
        raise CaptchaDetectedError(f"CAPTCHA detected via '{match}' on {page.url}")

