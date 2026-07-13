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


def test_audit_script_has_no_delete_mode():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "confirm-delete-invalid" not in source
    assert "--backup-file" not in source
    assert 'method="DELETE"' not in source
