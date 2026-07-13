from __future__ import annotations

import importlib.util
import urllib.error
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "audit_grok_accounts.py"
SPEC = importlib.util.spec_from_file_location("audit_grok_accounts_test", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_classify_completed_json_and_sse():
    assert MODULE.classify(200, '{"success":true}') == ("usable", "TEST_COMPLETED")
    body = 'data: {"type":"test_complete","success":true}\n\n'
    assert MODULE.classify(200, body) == ("usable", "TEST_COMPLETED")


def test_classify_quota_as_usable_exhausted():
    assert MODULE.classify(429, '{"code":"subscription:free-usage-exhausted"}') == (
        "usable_exhausted", "RATE_LIMITED",
    )
    assert MODULE.classify(402, 'spending-limit') == ("usable_exhausted", "RATE_LIMITED")


def test_classify_only_permanent_failures_as_invalid():
    assert MODULE.classify(401, 'invalid_grant: Refresh token has been revoked')[0] == "token_invalid"
    assert MODULE.classify(403, 'permission-denied: Access to the chat endpoint is denied')[0] == "permission_denied"
    assert MODULE.classify(401, 'invalid credentials') == ("transient_error", "TOKEN_REFRESH_REQUIRED")
    assert MODULE.classify(403, 'temporary forbidden')[0] == "transient_error"
    assert MODULE.classify(502, 'bad gateway')[0] == "transient_error"


def test_delete_mode_requires_backup_and_confirmation():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "confirm-delete-invalid" in source
    assert "--backup-file" in source
    assert "confirmation_mismatch" in source
    assert 'method="DELETE"' in source


def test_delete_account_requires_404_recheck(monkeypatch):
    methods = []

    def open_request(request, timeout):
        methods.append(request.get_method())
        if request.get_method() == "GET":
            raise urllib.error.HTTPError(request.full_url, 404, "not found", {}, None)

        class Response:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return b""

        return Response()

    monkeypatch.setattr(MODULE.urllib.request, "urlopen", open_request)
    assert MODULE.delete_account("http://sub2api", "secret", 123, 1) is True
    assert methods == ["DELETE", "GET"]
