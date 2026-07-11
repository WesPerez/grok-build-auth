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
    base_url = "http://grok-cli-proxy:8080/v1"
    bundle = MODULE.build_bundle([write_auth(tmp_path / "auth.json")], base_url)
    assert bundle["exported_at"].endswith("Z")
    assert bundle["accounts"][0]["extra"]["access_token_sha256"]
    assert bundle["accounts"][0]["credentials"]["base_url"] == base_url
    assert bundle["accounts"][0]["extra"]["base_url"] == base_url


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


def test_helper_env_removes_proxy_for_loopback():
    env = MODULE.helper_env({"SUB2API_URL": "http://127.0.0.1:13080"})
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        assert key not in env
    assert "127.0.0.1" in env["NO_PROXY"]


def test_helper_env_rejects_remote_sub2api():
    with pytest.raises(MODULE.BatchError, match="loopback"):
        MODULE.helper_env({"SUB2API_URL": "https://example.com"})


def test_resume_bundle_requires_matching_hash_and_exact_path(tmp_path):
    batch = tmp_path / "runs" / "batch-1"
    auth_dir = batch / "auth" / "xaiabcdef"
    bundle_dir = batch / "bundle"
    auth_dir.mkdir(parents=True)
    bundle_dir.mkdir()
    auth = write_auth(auth_dir / "xaiabcdef@example.com.json")
    bundle = bundle_dir / "sub2api-bundle.json"
    MODULE.atomic_json(bundle, MODULE.build_bundle([auth], "http://grok-cli-proxy:8080/v1"))
    MODULE.atomic_json(batch / "manifest.json", {
        "batch_id": "batch-1", "status": "import-failed", "bundle": str(bundle),
        "bundle_sha256": MODULE.hashlib.sha256(bundle.read_bytes()).hexdigest(),
        "attempts": [{"status": "registered", "auth_file": str(auth)}],
    })
    manifest, loaded_bundle, auth_paths = MODULE.load_resume_bundle(batch)
    assert manifest["batch_id"] == "batch-1"
    assert loaded_bundle == bundle.resolve()
    assert auth_paths == [auth.resolve()]


def test_grok_target_config_requires_dedicated_group_and_proxy():
    assert MODULE.validate_grok_target_config({
        "SUB2API_GROUP": "grok",
        "GROK_ACCOUNT_BASE_URL": "http://grok-cli-proxy:8080/v1",
    }) == "http://grok-cli-proxy:8080/v1"
    with pytest.raises(MODULE.BatchError, match="must be grok"):
        MODULE.validate_grok_target_config({
            "SUB2API_GROUP": "openai",
            "GROK_ACCOUNT_BASE_URL": "http://grok-cli-proxy:8080/v1",
        })
    with pytest.raises(MODULE.BatchError, match="grok-cli-proxy"):
        MODULE.validate_grok_target_config({
            "SUB2API_GROUP": "grok",
            "GROK_ACCOUNT_BASE_URL": "https://cli-chat-proxy.grok.com/v1",
        })


def test_reconcile_updates_exact_ids_through_admin_api(monkeypatch):
    updated = []
    monkeypatch.setattr(MODULE, "grok_group_id", lambda config: 5)
    monkeypatch.setattr(MODULE, "make_admin_token", lambda config: "short-lived-token")
    monkeypatch.setattr(
        MODULE, "update_account_via_admin_api",
        lambda config, token, account_id, group_id: updated.append((token, account_id, group_id)),
    )
    MODULE.reconcile_imported_accounts({
        "SUB2API_POSTGRES_CONTAINER": "postgres",
        "SUB2API_PG_USER": "user",
        "SUB2API_PG_DB": "db",
        "SUB2API_GROUP": "grok",
        "GROK_ACCOUNT_BASE_URL": "http://grok-cli-proxy:8080/v1",
    }, [17, 23])
    assert updated == [("short-lived-token", 17, 5), ("short-lived-token", 23, 5)]


def test_build_bundle_rejects_duplicate_auth_tokens(tmp_path):
    first = write_auth(tmp_path / "one.json", "xai111111@example.com")
    second = write_auth(tmp_path / "two.json", "xai222222@example.com")
    with pytest.raises(MODULE.BatchError, match="duplicate"):
        MODULE.build_bundle([first, second], "http://grok-cli-proxy:8080/v1")
