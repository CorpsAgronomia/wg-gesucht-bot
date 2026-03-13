from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from bot.session_manager import SessionData, SessionManagerError, load_session, login_via_api, refresh_session_via_api, save_session


class SessionManagerTest(unittest.TestCase):
    def test_session_round_trip(self) -> None:
        session = SessionData(
            cookies=[{"name": "sessionid", "value": "abc123", "domain": ".wg-gesucht.de", "path": "/"}],
            csrf_token="csrf-token",
            user_agent="UnitTestAgent/1.0",
            captured_at="2026-03-12T00:00:00+00:00",
            access_token="access-token",
            refresh_token="refresh-token",
            client_id="client-id",
            dev_ref_no="dev-ref",
            user_id="12345678",
            login_token="login-token",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            settings = SimpleNamespace(session_file=session_path, user_agent=session.user_agent)
            save_session(session, settings=settings)
            loaded = load_session(settings=settings)

        self.assertEqual(loaded.cookies, session.cookies)
        self.assertEqual(loaded.csrf_token, session.csrf_token)
        self.assertEqual(loaded.user_agent, session.user_agent)
        self.assertEqual(loaded.access_token, session.access_token)
        self.assertEqual(loaded.user_id, session.user_id)


class SessionRefreshApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_session_via_api_renews_tokens(self) -> None:
        session = SessionData(
            cookies=[
                {"name": "X-Access-Token", "value": "old-access", "domain": ".wg-gesucht.de", "path": "/"},
                {"name": "X-Refresh-Token", "value": "old-refresh", "domain": ".wg-gesucht.de", "path": "/"},
                {"name": "X-Dev-Ref-No", "value": "old-dev-ref", "domain": ".wg-gesucht.de", "path": "/"},
                {"name": "login_token", "value": "old-login", "domain": ".wg-gesucht.de", "path": "/"},
            ],
            csrf_token="old-csrf",
            user_agent="UnitTestAgent/1.0",
            captured_at="2026-03-12T00:00:00+00:00",
            access_token="old-access",
            refresh_token="old-refresh",
            client_id="wg_desktop_website",
            dev_ref_no="old-dev-ref",
            user_id="12345678",
            login_token="old-login",
        )
        settings = SimpleNamespace(request_timeout_seconds=10, user_agent=session.user_agent)

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs) -> None:
                self.cookies = kwargs["cookies"]

            async def __aenter__(self):
                self.cookies.set("X-Access-Token", "new-access", domain=".wg-gesucht.de", path="/")
                self.cookies.set("X-Refresh-Token", "new-refresh", domain=".wg-gesucht.de", path="/")
                self.cookies.set("X-Dev-Ref-No", "new-dev-ref", domain=".wg-gesucht.de", path="/")
                self.cookies.set("login_token", "new-login", domain=".wg-gesucht.de", path="/")
                return self

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

            async def put(self, url: str, headers: dict[str, str]) -> httpx.Response:
                payload = {
                    "status": 200,
                    "detail": {
                        "access_token": "new-access",
                        "refresh_token": "new-refresh",
                        "csrf_token": "new-csrf",
                        "token_type": "Bearer",
                        "user_id": "12345678",
                        "dev_ref_no": "new-dev-ref",
                    },
                }
                return httpx.Response(200, text=json.dumps(payload))

        with patch("bot.session_manager.httpx.AsyncClient", FakeAsyncClient):
            refreshed = await refresh_session_via_api(session, settings=settings, logger=logging.getLogger("test"))

        self.assertEqual(refreshed.access_token, "new-access")
        self.assertEqual(refreshed.refresh_token, "new-refresh")
        self.assertEqual(refreshed.csrf_token, "new-csrf")
        self.assertEqual(refreshed.dev_ref_no, "new-dev-ref")
        self.assertEqual(refreshed.login_token, "new-login")

    async def test_refresh_session_via_api_rejects_invalid_response(self) -> None:
        session = SessionData(
            cookies=[
                {"name": "X-Access-Token", "value": "old-access", "domain": ".wg-gesucht.de", "path": "/"},
                {"name": "X-Refresh-Token", "value": "old-refresh", "domain": ".wg-gesucht.de", "path": "/"},
                {"name": "X-Dev-Ref-No", "value": "old-dev-ref", "domain": ".wg-gesucht.de", "path": "/"},
            ],
            csrf_token="old-csrf",
            user_agent="UnitTestAgent/1.0",
            captured_at="2026-03-12T00:00:00+00:00",
            access_token="old-access",
            refresh_token="old-refresh",
            client_id="wg_desktop_website",
            dev_ref_no="old-dev-ref",
            user_id="12345678",
            login_token="",
        )
        settings = SimpleNamespace(request_timeout_seconds=10, user_agent=session.user_agent)

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs) -> None:
                self.cookies = kwargs["cookies"]

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

            async def put(self, url: str, headers: dict[str, str]) -> httpx.Response:
                return httpx.Response(200, text="not-json")

        with patch("bot.session_manager.httpx.AsyncClient", FakeAsyncClient):
            with self.assertRaises(SessionManagerError):
                await refresh_session_via_api(session, settings=settings, logger=logging.getLogger("test"))

    async def test_refresh_session_via_api_accepts_empty_success_response(self) -> None:
        session = SessionData(
            cookies=[
                {"name": "X-Access-Token", "value": "old-access", "domain": ".wg-gesucht.de", "path": "/"},
                {"name": "X-Refresh-Token", "value": "old-refresh", "domain": ".wg-gesucht.de", "path": "/"},
                {"name": "X-Dev-Ref-No", "value": "old-dev-ref", "domain": ".wg-gesucht.de", "path": "/"},
                {"name": "login_token", "value": "old-login", "domain": ".wg-gesucht.de", "path": "/"},
            ],
            csrf_token="old-csrf",
            user_agent="UnitTestAgent/1.0",
            captured_at="2026-03-12T00:00:00+00:00",
            access_token="old-access",
            refresh_token="old-refresh",
            client_id="wg_desktop_website",
            dev_ref_no="old-dev-ref",
            user_id="12345678",
            login_token="old-login",
        )
        settings = SimpleNamespace(request_timeout_seconds=10, user_agent=session.user_agent)

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs) -> None:
                self.cookies = kwargs["cookies"]

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

            async def put(self, url: str, headers: dict[str, str]) -> httpx.Response:
                return httpx.Response(200, text="")

        with patch("bot.session_manager.httpx.AsyncClient", FakeAsyncClient):
            refreshed = await refresh_session_via_api(session, settings=settings, logger=logging.getLogger("test"))

        self.assertEqual(refreshed.access_token, session.access_token)
        self.assertEqual(refreshed.refresh_token, session.refresh_token)
        self.assertEqual(refreshed.csrf_token, session.csrf_token)
        self.assertEqual(refreshed.dev_ref_no, session.dev_ref_no)


class SessionLoginApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_login_via_api_captures_tokens_and_cookies(self) -> None:
        settings = SimpleNamespace(
            request_timeout_seconds=10,
            user_agent="UnitTestAgent/1.0",
            base_url="https://www.wg-gesucht.de/",
            email="user@example.com",
            password="secret",
        )

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs) -> None:
                self.cookies = httpx.Cookies()

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

            async def get(self, url: str) -> httpx.Response:
                self.cookies.set("X-Client-Id", "wg_desktop_website", domain=".wg-gesucht.de", path="/")
                return httpx.Response(200, text="ok")

            async def post(self, url: str, headers: dict[str, str], content: str) -> httpx.Response:
                self.cookies.set("X-Access-Token", "new-access", domain=".wg-gesucht.de", path="/")
                self.cookies.set("X-Refresh-Token", "new-refresh", domain=".wg-gesucht.de", path="/")
                self.cookies.set("X-Dev-Ref-No", "new-dev-ref", domain=".wg-gesucht.de", path="/")
                self.cookies.set("login_token", "new-login", domain=".wg-gesucht.de", path="/")
                self.cookies.set("dev_ref_no", "new-dev-ref", domain=".wg-gesucht.de", path="/")
                payload = {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "token_type": "Bearer",
                    "user_id": "12345678",
                    "dev_ref_no": "new-dev-ref",
                    "csrf_token": "new-csrf",
                }
                return httpx.Response(200, text=json.dumps(payload))

        with patch("bot.session_manager.httpx.AsyncClient", FakeAsyncClient):
            session = await login_via_api(settings=settings, logger=logging.getLogger("test"))

        self.assertEqual(session.access_token, "new-access")
        self.assertEqual(session.refresh_token, "new-refresh")
        self.assertEqual(session.user_id, "12345678")
        self.assertEqual(session.dev_ref_no, "new-dev-ref")
        self.assertEqual(session.csrf_token, "new-csrf")
        self.assertEqual(session.login_token, "new-login")


if __name__ == "__main__":
    unittest.main()
