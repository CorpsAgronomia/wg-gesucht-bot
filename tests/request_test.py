from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bot.bump_api import load_request_template
from bot.request_client import RequestClient
from bot.request_templates import resolve_template_path, template_path_for_listing
from bot.response_validator import _extract_timestamp
from bot.session_manager import SessionData


class RequestTemplateTest(unittest.TestCase):
    def test_request_template_is_valid_and_rendered(self) -> None:
        template = {
            "endpoint": "https://example.com/listings/{listing_id}/update",
            "method": "POST",
            "headers": {
                "content-type": "application/json",
                "x-csrf-token": "{csrf_token}",
            },
            "body_template": {
                "encoding": "json",
                "json": {
                    "listingId": "{listing_id}",
                    "_csrf": "{csrf_token}",
                },
            },
            "required_cookies": ["sessionid"],
            "csrf_field": "x-csrf-token",
            "csrf_body_field": "_csrf",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            template_path = Path(temp_dir) / "update_request_template.json"
            template_path.write_text(json.dumps(template), encoding="utf-8")
            loaded = load_request_template(template_path)

        session = SessionData(
            cookies=[{"name": "sessionid", "value": "abc", "domain": ".example.com", "path": "/"}],
            csrf_token="csrf-value",
            user_agent="UnitTestAgent/1.0",
            captured_at="",
            access_token="access-value",
            refresh_token="refresh-value",
            client_id="client-id",
            dev_ref_no="dev-ref",
            user_id="9001",
            login_token="login-token",
        )
        settings = SimpleNamespace(
            request_timeout_seconds=10,
            retry_attempts=5,
            retry_backoff_multiplier=2,
            retry_backoff_min_seconds=1,
            retry_backoff_max_seconds=5,
        )
        client = RequestClient(settings=settings)
        prepared = client.prepare_request(loaded, session, listing_id="42")

        self.assertEqual(prepared.url, "https://example.com/listings/42/update")
        self.assertEqual(prepared.method, "POST")
        self.assertEqual(prepared.headers["x-csrf-token"], "csrf-value")
        self.assertEqual(prepared.json_body["listingId"], "42")
        self.assertEqual(prepared.json_body["_csrf"], "csrf-value")

    def test_last_updated_timestamp_is_extracted_from_listing_page(self) -> None:
        html = """
        <div class="last_updated_info text-center mb30">
            <span class="font-12px text-medium-gray">
                Zuletzt aktualisiert:
                <span class="last_updated_date">12.03.2026 - 22:43</span>
            </span>
        </div>
        """

        self.assertEqual(_extract_timestamp(html), "12.03.2026 - 22:43")

    def test_multi_listing_template_resolution_requires_listing_specific_template(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            settings = SimpleNamespace(
                listing_ids=("12188101", "13127492"),
                request_template_path=temp_path / "update_request_template.json",
                request_templates_dir=temp_path / "update_request_templates",
            )
            settings.request_templates_dir.mkdir(parents=True, exist_ok=True)
            settings.request_template_path.write_text("{}", encoding="utf-8")

            first_listing_path = template_path_for_listing(settings, "12188101")
            first_listing_path.write_text("{}", encoding="utf-8")

            self.assertEqual(resolve_template_path(settings, "12188101"), first_listing_path)
            with self.assertRaises(FileNotFoundError):
                resolve_template_path(settings, "13127492")


if __name__ == "__main__":
    unittest.main()
