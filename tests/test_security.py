from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xconsole_client.security import (
    mask_email,
    redact_text,
    secure_json_write,
    validate_cliproxyapi_base_url,
    validate_loopback_host,
)
from xconsole_client.xai_oauth import (
    build_cliproxyapi_auth_record,
    complete_build_oauth,
    login_with_device_browser,
)
from xconsole_client.client import XConsoleAuthClient


class SecurityTests(unittest.TestCase):
    @staticmethod
    def _jwt(payload: dict) -> str:
        def encode(value: dict) -> str:
            raw = json.dumps(value, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        return f"{encode({'alg': 'none'})}.{encode(payload)}."

    def test_secure_json_write_permissions_and_replace(self):
        with tempfile.TemporaryDirectory() as td:
            old = os.umask(0)
            try:
                path = secure_json_write(Path(td) / "auth" / "xai-test.json", {"token": "one"})
                secure_json_write(path, {"token": "two"})
            finally:
                os.umask(old)
            self.assertEqual(json.loads(path.read_text()), {"token": "two"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_base_url_allowlist(self):
        self.assertEqual(
            validate_cliproxyapi_base_url("https://cli-chat-proxy.grok.com/v1/"),
            "https://cli-chat-proxy.grok.com/v1",
        )
        rejected = [
            "http://cli-chat-proxy.grok.com/v1",
            "https://evil.example/v1",
            "https://cli-chat-proxy.grok.com.evil/v1",
            "https://user:pass@cli-chat-proxy.grok.com/v1",
            "https://cli-chat-proxy.grok.com:444/v1",
            "https://cli-chat-proxy.grok.com/v1?q=secret",
        ]
        for value in rejected:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_cliproxyapi_base_url(value)

    def test_library_record_builder_enforces_allowlist(self):
        with self.assertRaises(ValueError):
            build_cliproxyapi_auth_record({"access_token": "secret"}, base_url="https://evil.example/v1")

    def test_device_browser_mint_writes_only_verified_identity(self):
        captured_kwargs = {}

        def mint_with_browser(**kwargs):
            captured_kwargs.update(kwargs)
            kwargs["poll_log"]("device user_code=PRIVATE-CODE")
            kwargs["poll_log"]("standalone chromium started")
            kwargs["poll_log"]("token poll SUCCESS - stop_event set")
            return {
                "access_token": "a" * 40,
                "refresh_token": "r" * 40,
                "id_token": self._jwt({"email": "owner@example.com", "sub": "subject-1"}),
                "expires_in": 3600,
                "user_code": "PRIVATE-CODE",
            }

        fake_oidc = types.SimpleNamespace(mint_with_browser=mint_with_browser)
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            sys.modules, {"oidc_mint": fake_oidc}
        ), mock.patch(
            "xconsole_client.xai_oauth.fetch_userinfo",
            return_value={"email": "owner@example.com", "sub": "subject-1"},
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = login_with_device_browser(
                    "owner@example.com",
                    "password",
                    cliproxyapi_auth_dir=td,
                    proxy="socks5://127.0.0.1:10902",
                    debug=True,
                )

            record = json.loads(Path(result.cliproxyapi_path).read_text(encoding="utf-8"))

        self.assertEqual(record["email"], "owner@example.com")
        self.assertEqual(record["sub"], "subject-1")
        self.assertNotIn("user_code", result.token)
        self.assertNotIn("PRIVATE-CODE", output.getvalue())
        self.assertIn("device-browser stage=browser_started", output.getvalue())
        self.assertFalse(captured_kwargs["headless"])
        self.assertFalse(captured_kwargs["reuse_browser"])

    def test_complete_build_oauth_can_select_device_flow_without_protocol_replay(self):
        marker = object()
        with mock.patch(
            "xconsole_client.xai_oauth.login_with_device_browser", return_value=marker
        ) as device, mock.patch(
            "xconsole_client.xai_oauth.login_with_playwright"
        ) as playwright:
            result = complete_build_oauth(
                "owner@example.com",
                "password",
                protocol=False,
                device_browser_fallback=True,
            )

        self.assertIs(result, marker)
        device.assert_called_once()
        playwright.assert_not_called()

    def test_complete_build_oauth_rejects_multiple_browser_fallbacks(self):
        with mock.patch(
            "xconsole_client.xai_oauth.login_with_device_browser"
        ) as device, mock.patch(
            "xconsole_client.xai_oauth.login_with_playwright"
        ) as playwright:
            with self.assertRaisesRegex(ValueError, "mutually exclusive"):
                complete_build_oauth(
                    "owner@example.com",
                    "password",
                    protocol=False,
                    device_browser_fallback=True,
                    playwright_fallback=True,
                )

        device.assert_not_called()
        playwright.assert_not_called()

    def test_device_browser_failure_emits_only_safe_terminal_category(self):
        def mint_with_browser(**kwargs):
            kwargs["poll_log"]("standalone chromium started")
            kwargs["poll_log"]("login attempt 1 for PRIVATE-CODE")
            raise RuntimeError("device auth failed: access_denied: PRIVATE-CODE")

        fake_oidc = types.SimpleNamespace(mint_with_browser=mint_with_browser)
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {"oidc_mint": fake_oidc}), contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, "terminal=access_denied"):
                login_with_device_browser(
                    "owner@example.com",
                    "password",
                    proxy="socks5://127.0.0.1:10902",
                    debug=True,
                )

        self.assertIn("device-browser stage=browser_started", output.getvalue())
        self.assertIn("device-browser stage=device_login", output.getvalue())
        self.assertIn("device-browser terminal=access_denied", output.getvalue())
        self.assertNotIn("PRIVATE-CODE", output.getvalue())

    def test_loopback_allowlist(self):
        self.assertEqual(validate_loopback_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(validate_loopback_host("::1"), "::1")
        for host in ("localhost", "0.0.0.0", "192.168.1.2"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                validate_loopback_host(host)

    def test_redaction(self):
        secret = "header.payload.signature-long"
        text = redact_text(
            f'access_token=abc123 password=hunter2 url=https://user:pass@example.test/path?code=abc '
            f'sso={secret} response={{"code":"oauth-code-value","refresh_token":"refresh-value"}}'
        )
        for value in (
            "abc123",
            "hunter2",
            "user:pass",
            "?code=abc",
            secret,
            "oauth-code-value",
            "refresh-value",
        ):
            self.assertNotIn(value, text)
        self.assertEqual(mask_email("alice@example.com"), "a***@example.com")

    def test_sso_is_not_persisted_when_save_is_false(self):
        client = object.__new__(XConsoleAuthClient)
        client.debug = False
        client._last_create_set_cookies = []
        client._last_rsc_body = ""
        client._fetch_sso_via_grok_home = lambda: "memory-only-token"
        client._read_sso_from_jar = lambda: None
        with mock.patch("xconsole_client.sso.save_sso") as save_sso:
            token = client.fetch_sso_token(
                email="owner@example.com", password="secret", save=False, retries=1
            )
        self.assertEqual(token, "memory-only-token")
        save_sso.assert_not_called()


if __name__ == "__main__":
    unittest.main()
