from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from time import monotonic
from typing import Callable, TypeAlias

from playwright.async_api import Locator, Page

from bot.logger import log_event
from bot.retry import build_async_retry

Scope: TypeAlias = Page | Locator
LocatorFactory: TypeAlias = Callable[[Scope], Locator]


class SelectorResolutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SelectorGroup:
    name: str
    factories: tuple[LocatorFactory, ...]


def role(role_name: str, accessible_name) -> LocatorFactory:
    return lambda scope: scope.get_by_role(role_name, name=accessible_name)


def text(value, *, exact: bool = False) -> LocatorFactory:
    return lambda scope: scope.get_by_text(value, exact=exact)


def css(selector: str) -> LocatorFactory:
    return lambda scope: scope.locator(selector)


def placeholder(value) -> LocatorFactory:
    return lambda scope: scope.get_by_placeholder(value)


def label(value, *, exact: bool = False) -> LocatorFactory:
    return lambda scope: scope.get_by_label(value, exact=exact)


def has_text_selector(selector: str, value: str) -> LocatorFactory:
    return lambda scope: scope.locator(selector).filter(has_text=value)


async def _first_visible_locator(locator: Locator, timeout_ms: int) -> Locator:
    deadline = monotonic() + (timeout_ms / 1000)
    last_error: Exception | None = None

    while monotonic() < deadline:
        try:
            count = await locator.count()
            for index in range(count):
                candidate = locator.nth(index)
                if await candidate.is_visible():
                    return candidate
        except Exception as exc:
            last_error = exc

        await asyncio.sleep(0.1)

    if last_error is not None:
        raise last_error
    raise TimeoutError(f"No visible elements found within {timeout_ms}ms.")


async def resolve(
    scope: Scope,
    group: SelectorGroup,
    *,
    settings,
    logger,
    timeout_ms: int | None = None,
) -> Locator:
    effective_timeout = timeout_ms or settings.selector_timeout_ms
    per_selector_timeout = max(1000, effective_timeout // max(1, len(group.factories)))
    last_error: Exception | None = None
    retrying = build_async_retry(settings, attempts=settings.selector_retry_attempts)

    async for attempt in retrying:
        with attempt:
            for factory in group.factories:
                locator = factory(scope)
                try:
                    return await _first_visible_locator(locator, per_selector_timeout)
                except Exception as exc:
                    last_error = exc

            log_event(
                logger,
                "selector_retry",
                status="retrying",
                component="selectors",
                level=logging.WARNING,
                selector=group.name,
                attempt=attempt.retry_state.attempt_number,
            )
            raise SelectorResolutionError(
                f"Unable to resolve selector '{group.name}'. Last error: {last_error}"
            )

    raise SelectorResolutionError(f"Unable to resolve selector '{group.name}'.")


async def resolve_optional(
    scope: Scope,
    group: SelectorGroup,
    *,
    settings,
    logger,
    timeout_ms: int | None = None,
) -> Locator | None:
    effective_timeout = timeout_ms or settings.selector_timeout_ms
    per_selector_timeout = max(750, effective_timeout // max(1, len(group.factories)))

    for factory in group.factories:
        locator = factory(scope)
        try:
            return await _first_visible_locator(locator, per_selector_timeout)
        except Exception:
            continue
    return None


async def click(scope: Scope, group: SelectorGroup, *, settings, logger, timeout_ms: int | None = None) -> Locator:
    locator = await resolve(scope, group, settings=settings, logger=logger, timeout_ms=timeout_ms)
    await locator.click(timeout=timeout_ms or settings.action_timeout_ms)
    return locator


async def click_optional(
    scope: Scope,
    group: SelectorGroup,
    *,
    settings,
    logger,
    timeout_ms: int | None = None,
) -> bool:
    locator = await resolve_optional(scope, group, settings=settings, logger=logger, timeout_ms=timeout_ms)
    if locator is None:
        return False
    await locator.click(timeout=timeout_ms or settings.action_timeout_ms)
    return True


async def fill(
    scope: Scope,
    group: SelectorGroup,
    value: str,
    *,
    settings,
    logger,
    timeout_ms: int | None = None,
) -> Locator:
    locator = await resolve(scope, group, settings=settings, logger=logger, timeout_ms=timeout_ms)
    await locator.fill(value, timeout=timeout_ms or settings.action_timeout_ms)
    return locator


async def is_visible(
    scope: Scope,
    group: SelectorGroup,
    *,
    settings,
    logger,
    timeout_ms: int | None = None,
) -> bool:
    locator = await resolve_optional(scope, group, settings=settings, logger=logger, timeout_ms=timeout_ms)
    return locator is not None


async def extract_text_optional(
    scope: Scope,
    group: SelectorGroup,
    *,
    settings,
    logger,
    timeout_ms: int | None = None,
) -> str | None:
    locator = await resolve_optional(scope, group, settings=settings, logger=logger, timeout_ms=timeout_ms)
    if locator is None:
        return None
    text_value = await locator.inner_text()
    stripped = text_value.strip()
    return stripped or None


async def wait_for_any(
    scope: Scope,
    groups: tuple[SelectorGroup, ...],
    *,
    settings,
    logger,
    timeout_ms: int | None = None,
) -> tuple[SelectorGroup, Locator] | None:
    for group in groups:
        locator = await resolve_optional(scope, group, settings=settings, logger=logger, timeout_ms=timeout_ms)
        if locator is not None:
            return group, locator
    return None


COOKIE_ACCEPT = SelectorGroup(
    "cookie_accept",
    (
        role("button", re.compile(r"Accept all|Alle akzeptieren|Akzeptieren", re.IGNORECASE)),
        text(re.compile(r"Accept all|Alle akzeptieren", re.IGNORECASE)),
        css("button[aria-label*='Accept' i]"),
        css("button[title*='Accept' i]"),
    ),
)

ACCOUNT_MENU = SelectorGroup(
    "account_menu",
    (
        role("link", re.compile(r"Mein Konto|Mein WG-Gesucht", re.IGNORECASE)),
        role("button", re.compile(r"Mein Konto|Mein WG-Gesucht", re.IGNORECASE)),
        text(re.compile(r"Mein Konto|Mein WG-Gesucht", re.IGNORECASE)),
        css("a[href='#'][title*='Konto' i]"),
    ),
)

MY_LISTINGS = SelectorGroup(
    "my_listings",
    (
        role("link", re.compile(r"Meine Anzeigen", re.IGNORECASE)),
        role("button", re.compile(r"Meine Anzeigen", re.IGNORECASE)),
        text(re.compile(r"Meine Anzeigen", re.IGNORECASE)),
        css("a[href*='anzeigen' i]"),
    ),
)

LOGOUT_LINK = SelectorGroup(
    "logout_link",
    (
        role("link", re.compile(r"Abmelden|Logout", re.IGNORECASE)),
        text(re.compile(r"Abmelden|Logout", re.IGNORECASE)),
        css("a[href*='logout' i]"),
    ),
)

LOGIN_EMAIL = SelectorGroup(
    "login_email",
    (
        css("#login_email_username"),
        css("#cu_email"),
        css("input[name='login_email_username']"),
        label(re.compile(r"E-?Mail", re.IGNORECASE)),
        placeholder("E-Mail-Adresse"),
        css("input[type='email']"),
        css("input[name='email']"),
    ),
)

LOGIN_PASSWORD = SelectorGroup(
    "login_password",
    (
        css("#login_password"),
        css("#cu_password"),
        css("input[name='login_password']"),
        label(re.compile(r"Passwort", re.IGNORECASE)),
        placeholder("Passwort"),
        css("input[type='password']"),
        css("input[name='password']"),
    ),
)

LOGIN_BUTTON = SelectorGroup(
    "login_button",
    (
        role("button", re.compile(r"^Login$", re.IGNORECASE)),
        text(re.compile(r"^Login$", re.IGNORECASE)),
        css("button[type='submit']"),
    ),
)

REMEMBER_ME = SelectorGroup(
    "remember_me",
    (
        label(re.compile(r"Angemeldet bleiben", re.IGNORECASE)),
        role("checkbox", re.compile(r"Angemeldet bleiben", re.IGNORECASE)),
        text(re.compile(r"Angemeldet bleiben", re.IGNORECASE)),
        css("input[type='checkbox']"),
    ),
)

LOGIN_ERROR = SelectorGroup(
    "login_error",
    (
        text(re.compile(r"fehlgeschlagen|ungultig|ungültig|falsch|incorrect|invalid|wrong", re.IGNORECASE)),
        css(".alert-danger"),
        css(".alert-error"),
        css("[role='alert']"),
    ),
)

LISTING_OPTIONS_MENU = SelectorGroup(
    "listing_options_menu",
    (
        role("button", re.compile(r"Optionen|Mehr|Menu|Menü", re.IGNORECASE)),
        role("link", re.compile(r"Optionen|Mehr|Menu|Menü", re.IGNORECASE)),
        css("[aria-label*='Option' i]"),
        css("[aria-label*='Menü' i]"),
        css("[title*='Option' i]"),
    ),
)

EDIT_PHOTOS = SelectorGroup(
    "edit_photos",
    (
        role("button", re.compile(r"Bearbeiten\s*\+\s*Fotos|Bearbeiten.*Fotos|Edit.*Photos?", re.IGNORECASE)),
        role("link", re.compile(r"Bearbeiten\s*\+\s*Fotos|Bearbeiten.*Fotos|Edit.*Photos?", re.IGNORECASE)),
        text(re.compile(r"Bearbeiten\s*\+\s*Fotos|Bearbeiten.*Fotos|Edit.*Photos?", re.IGNORECASE)),
        css("a[href*='edit' i]"),
        css("button[data-testid*='edit' i]"),
    ),
)

UPDATE_AND_VIEW = SelectorGroup(
    "update_and_view",
    (
        role("button", re.compile(r"Aktualisieren und Ansehen|Update listing|Update and View", re.IGNORECASE)),
        role("link", re.compile(r"Aktualisieren und Ansehen|Update listing|Update and View", re.IGNORECASE)),
        text(re.compile(r"Aktualisieren und Ansehen|Update listing|Update and View", re.IGNORECASE)),
        css("button[id*='update' i]"),
        css("button[name*='update' i]"),
    ),
)

UPDATE_CONFIRMATION = SelectorGroup(
    "update_confirmation",
    (
        css("div[role='dialog'] button[data-bb-handler='confirm']"),
        css("div[role='dialog'] .modal-footer button.btn-primary"),
        css("div[role='dialog'] button[class*='confirm' i]"),
        css("div[role='dialog'] a[class*='confirm' i]"),
        role("button", re.compile(r"Bestätigen|Ja|OK|Okay|Confirm", re.IGNORECASE)),
        role("link", re.compile(r"Bestätigen|Ja|OK|Okay|Confirm", re.IGNORECASE)),
        css("button[data-bb-handler='confirm']"),
        css(".modal-footer button.btn-primary"),
    ),
)


def listing_target(target: str) -> SelectorGroup:
    escaped_target = re.escape(target)
    numeric_css = (
        css(f"[href*='{target}']"),
        css(f"[data-id*='{target}']"),
        css(f"[id*='{target}']"),
    ) if target.isdigit() else ()

    return SelectorGroup(
        f"listing_target:{target}",
        (
            role("link", re.compile(escaped_target, re.IGNORECASE)),
            text(target, exact=True),
            text(re.compile(escaped_target, re.IGNORECASE)),
            has_text_selector("article", target),
            has_text_selector("tr", target),
            has_text_selector("li", target),
            has_text_selector("div[data-id]", target),
            has_text_selector("div[class*='list' i]", target),
            has_text_selector("div[class*='card' i]", target),
        )
        + numeric_css,
    )
