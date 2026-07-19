from __future__ import annotations

import json
import os
import stat
import tempfile
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
from xconsole_client.xai_oauth import build_cliproxyapi_auth_record
from xconsole_client.client import XConsoleAuthClient


class SecurityTests(unittest.TestCase):
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
