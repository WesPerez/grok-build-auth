import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "register_and_import.py"
SPEC = importlib.util.spec_from_file_location("register_and_import", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def write_auth(path: Path, email: str = "xaiabcdef@example.com") -> Path:
    path.write_text(json.dumps({
        "email": email,
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "base_url": "https://cli-chat-proxy.grok.com/v1",
    }), encoding="utf-8")
    return path


def test_build_bundle_uses_current_timestamp_and_token_hash(tmp_path):
    bundle = MODULE.build_bundle([write_auth(tmp_path / "auth.json")])
    assert bundle["exported_at"].endswith("Z")
    assert bundle["accounts"][0]["extra"]["access_token_sha256"]


def test_load_single_result_rejects_auth_outside_attempt_dir(tmp_path):
    auth_dir = tmp_path / "attempt"
    auth_dir.mkdir()
    auth = write_auth(tmp_path / "other.json")
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"results": [{
        "email": "xaiabcdef@example.com",
        "error": None,
        "cliproxyapi_auth": str(auth),
    }]}), encoding="utf-8")
    with pytest.raises(MODULE.BatchError, match="outside"):
        MODULE.load_single_result(result, "xaiabcdef@example.com", auth_dir)


def test_validate_import_output_rejects_partial_failure():
    payload = {
        "import": {"response": {"data": {"account_created": 1, "account_failed": 1}}},
        "imported_ids": [1],
        "verification": {"imported_active": 1, "imported_bound_group": 1},
    }
    with pytest.raises(MODULE.BatchError, match="count mismatch"):
        MODULE.validate_import_output(payload, 2)


def test_validate_batch_email_is_exact():
    assert MODULE.validate_batch_email("xaiabcdef@example.com", "example.com") == (
        "xaiabcdef", "example.com"
    )
    with pytest.raises(MODULE.BatchError):
        MODULE.validate_batch_email("admin@example.com", "example.com")


def test_unknown_upstream_state_never_authorizes_cleanup(tmp_path):
    assert MODULE.upstream_account_created(tmp_path / "missing.json") is None
    broken = tmp_path / "broken.json"
    broken.write_text("not-json", encoding="utf-8")
    assert MODULE.upstream_account_created(broken) is None


def test_explicit_false_upstream_state_can_be_identified(tmp_path):
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"results": [{"account_created": False}]}), encoding="utf-8")
    assert MODULE.upstream_account_created(result) is False
