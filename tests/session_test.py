from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bot.session_manager import SessionData, load_session, save_session


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


if __name__ == "__main__":
    unittest.main()
