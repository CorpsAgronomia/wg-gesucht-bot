from __future__ import annotations

import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import httpx

from bot.config import Settings, load_settings
from bot.logger import log_event
from bot.retry import build_async_retry
from bot.session_manager import SessionData

VOLATILE_HEADERS = {
    "content-length",
    "cookie",
    "host",
    "connection",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
}


class RequestPreparationError(RuntimeError):
    pass


class RequestTransportError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class PreparedRequest:
    url: str
    method: str
    headers: dict[str, str]
    data: dict[str, Any] | None = None
    json_body: dict[str, Any] | list[Any] | None = None
    content: str | None = None


@dataclass(slots=True)
class RequestExecution:
    request: PreparedRequest
    response: httpx.Response | None
    latency_ms: float | None
    attempt: int
    dry_run: bool


def _get_logger(logger):
    return logger or logging.getLogger("wg_bump_bot")


def _render_placeholders(value: Any, *, listing_id: str, session: SessionData) -> Any:
    if isinstance(value, str):
        replacements = {
            "{listing_id}": listing_id,
            "{csrf_token}": session.csrf_token,
            "{csrf}": session.csrf_token,
            "{access_token}": session.access_token,
            "{refresh_token}": session.refresh_token,
            "{client_id}": session.client_id,
            "{dev_ref_no}": session.dev_ref_no,
            "{user_id}": session.user_id,
            "{login_token}": session.login_token,
        }
        rendered = value
        for placeholder, actual in replacements.items():
            rendered = rendered.replace(placeholder, actual)
        return rendered
    if isinstance(value, list):
        return [_render_placeholders(item, listing_id=listing_id, session=session) for item in value]
    if isinstance(value, dict):
        return {
            key: _render_placeholders(item, listing_id=listing_id, session=session)
            for key, item in value.items()
        }
    return value


def _cookies_for_httpx(session: SessionData) -> httpx.Cookies:
    cookies = httpx.Cookies()
    for cookie in session.cookies:
        name = str(cookie.get("name", "")).strip()
        value = str(cookie.get("value", ""))
        if not name:
            continue
        kwargs: dict[str, Any] = {}
        if cookie.get("domain"):
            kwargs["domain"] = str(cookie["domain"])
        if cookie.get("path"):
            kwargs["path"] = str(cookie["path"])
        cookies.set(name, value, **kwargs)
    return cookies


class RequestClient:
    def __init__(self, settings: Settings | None = None, logger=None) -> None:
        self.settings = settings or load_settings()
        self.logger = _get_logger(logger)

    def prepare_request(self, template: dict[str, Any], session: SessionData, *, listing_id: str) -> PreparedRequest:
        endpoint = str(template.get("endpoint", "")).strip()
        method = str(template.get("method", "")).strip().upper()
        if not endpoint or not method:
            raise RequestPreparationError("Request template is missing endpoint or method.")

        headers = {
            key: value
            for key, value in _render_placeholders(
                dict(template.get("headers", {})),
                listing_id=listing_id,
                session=session,
            ).items()
            if key.lower() not in VOLATILE_HEADERS
        }
        headers.setdefault("User-Agent", session.user_agent)

        body_template = dict(template.get("body_template", {}))
        encoding = str(body_template.get("encoding", "empty")).lower()
        csrf_field = str(template.get("csrf_field", "")).strip()
        csrf_header_name = str(template.get("csrf_header_name", "")).strip()
        csrf_body_field = str(template.get("csrf_body_field", "")).strip()

        if csrf_header_name:
            headers[csrf_header_name] = session.csrf_token
        elif csrf_field and (csrf_field.lower().startswith("x-") or csrf_field in headers):
            headers[csrf_field] = session.csrf_token

        prepared = PreparedRequest(
            url=str(
                _render_placeholders(endpoint, listing_id=listing_id, session=session)
            ),
            method=method,
            headers=headers,
        )

        if encoding == "form":
            data = _render_placeholders(
                dict(body_template.get("fields", {})),
                listing_id=listing_id,
                session=session,
            )
            target_key = csrf_body_field or (csrf_field if csrf_field in data else "")
            if target_key:
                data[target_key] = session.csrf_token
            return PreparedRequest(
                url=prepared.url,
                method=prepared.method,
                headers=prepared.headers,
                data=data,
            )

        if encoding == "json":
            json_body = _render_placeholders(
                body_template.get("json", {}),
                listing_id=listing_id,
                session=session,
            )
            if isinstance(json_body, dict):
                target_key = csrf_body_field or (csrf_field if csrf_field in json_body else "")
                if target_key:
                    json_body[target_key] = session.csrf_token
            return PreparedRequest(
                url=prepared.url,
                method=prepared.method,
                headers=prepared.headers,
                json_body=json_body,
            )

        if encoding == "raw":
            return PreparedRequest(
                url=prepared.url,
                method=prepared.method,
                headers=prepared.headers,
                content=str(
                    _render_placeholders(
                        body_template.get("raw", ""),
                        listing_id=listing_id,
                        session=session,
                    )
                ),
            )

        return prepared

    async def execute(self, prepared_request: PreparedRequest, session: SessionData, *, dry_run: bool) -> RequestExecution:
        if dry_run:
            log_event(
                self.logger,
                "request_dry_run",
                status="skipped",
                component="request_client",
                method=prepared_request.method,
                url=prepared_request.url,
                headers=prepared_request.headers,
                data=prepared_request.data,
                json=prepared_request.json_body,
                content=prepared_request.content,
            )
            return RequestExecution(
                request=prepared_request,
                response=None,
                latency_ms=None,
                attempt=1,
                dry_run=True,
            )

        retrying = build_async_retry(self.settings)

        async for attempt in retrying:
            with attempt:
                try:
                    start = perf_counter()
                    async with httpx.AsyncClient(
                        timeout=self.settings.request_timeout_seconds,
                        follow_redirects=True,
                        cookies=_cookies_for_httpx(session),
                    ) as client:
                        response = await client.request(
                            prepared_request.method,
                            prepared_request.url,
                            headers=prepared_request.headers,
                            data=prepared_request.data,
                            json=prepared_request.json_body,
                            content=prepared_request.content,
                        )
                    latency_ms = (perf_counter() - start) * 1000
                    if response.status_code >= 500:
                        raise RequestTransportError(
                            f"{prepared_request.method} {prepared_request.url} returned HTTP {response.status_code}"
                        )
                    return RequestExecution(
                        request=prepared_request,
                        response=response,
                        latency_ms=latency_ms,
                        attempt=attempt.retry_state.attempt_number,
                        dry_run=False,
                    )
                except (httpx.HTTPError, RequestTransportError) as exc:
                    log_event(
                        self.logger,
                        "request_attempt_failed",
                        status="retrying",
                        component="request_client",
                        level=logging.ERROR,
                        error=str(exc),
                        attempt=attempt.retry_state.attempt_number,
                        method=prepared_request.method,
                        url=prepared_request.url,
                    )
                    raise

        raise RequestTransportError(f"Unable to execute {prepared_request.method} {prepared_request.url}")

    async def send(
        self,
        template: dict[str, Any],
        session: SessionData,
        *,
        listing_id: str,
        dry_run: bool,
    ) -> RequestExecution:
        prepared_request = self.prepare_request(template, session, listing_id=listing_id)
        return await self.execute(prepared_request, session, dry_run=dry_run)

    async def get(self, url: str, session: SessionData, *, headers: dict[str, str] | None = None) -> httpx.Response:
        merged_headers = {"User-Agent": session.user_agent, **(headers or {})}
        async with httpx.AsyncClient(
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=True,
            cookies=_cookies_for_httpx(session),
        ) as client:
            return await client.get(url, headers=merged_headers)
