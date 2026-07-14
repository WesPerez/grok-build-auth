import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "finalize_grok_batch.py"
SPEC = importlib.util.spec_from_file_location("finalize_grok_batch", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_probe_classification_accepts_completed_and_explicit_quota():
    assert MODULE.classify_probe({"status": 200, "completed": True}) == "usable"
    assert MODULE.classify_probe({
        "status": 200,
        "completed": False,
        "error": "Grok returned 429 subscription:free-usage-exhausted rolling 24-hour window",
    }) == "usable_exhausted"


def test_probe_classification_rejects_transient_capacity():
    with pytest.raises(MODULE.FinalizeError, match="no decisive usable probe"):
        MODULE.classify_probe({
            "status": 200,
            "completed": False,
            "error": "Grok returned 429: service temporarily at capacity; retry shortly",
        })


def test_batch_path_is_exact_and_cannot_escape(tmp_path):
    private = tmp_path / "private"
    batch = private / "runs" / "20260714T120000Z-abcdef"
    batch.mkdir(parents=True)
    write_json(batch / "manifest.json", {})
    assert MODULE.resolve_batch_dir(private, batch.name) == batch.resolve()
    with pytest.raises(MODULE.FinalizeError):
        MODULE.resolve_batch_dir(private, "../escape")
    link = private / "runs" / "20260714T120001Z-fedcba"
    link.symlink_to(batch, target_is_directory=True)
    with pytest.raises(MODULE.FinalizeError, match="symlink"):
        MODULE.resolve_batch_dir(private, link.name)


def test_registered_sources_rejects_auth_outside_batch(tmp_path):
    batch = tmp_path / "20260714T120000Z-abcdef"
    (batch / "auth").mkdir(parents=True)
    (batch / "results").mkdir()
    (batch / "logs").mkdir()
    outside = write_json(tmp_path / "outside.json", {
        "email": "xaiabcdef@example.com", "access_token": "secret",
    })
    manifest = {"attempts": [{
        "status": "registered", "auth_file": str(outside),
        "result_file": str(batch / "results" / "xaiabcdef.json"),
    }]}
    with pytest.raises(MODULE.FinalizeError, match="outside"):
        MODULE.registered_sources(batch, manifest)


def test_exact_child_rejects_symlinked_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.json"
    target.write_text("{}", encoding="utf-8")
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(MODULE.FinalizeError, match="symlink"):
        MODULE.require_exact_child(linked_parent / target.name, linked_parent)


def test_commit_removes_only_handoff_sources_and_retains_results(tmp_path):
    batch = tmp_path / "20260714T120000Z-abcdef"
    auth = write_json(batch / "auth" / "xaiabcdef" / "auth.json", {
        "email": "xaiabcdef@example.com", "access_token": "secret",
    })
    result = write_json(batch / "results" / "xaiabcdef.json", {"password": "keep-for-remint"})
    log = batch / "logs" / "xaiabcdef.log"
    log.parent.mkdir(parents=True)
    log.write_text("registration log", encoding="utf-8")
    log.chmod(0o600)
    failed_result = write_json(batch / "results" / "xaifailed.json", {"password": "keep"})
    failed_log = batch / "logs" / "xaifailed.log"
    failed_log.write_text("failed", encoding="utf-8")
    failed_log.chmod(0o600)
    bundle_one = write_json(batch / "bundle" / "sub2api-bundle.json", {"token": "remove"})
    bundle_two = write_json(batch / "bundle" / "pending-sub2api-bundle.json", {"token": "remove"})
    helper_log = write_json(batch / "import" / "helper.log", {"imported_ids": [7]})
    backup = batch / "backup" / "pre.dump"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"dump")
    backup.chmod(0o600)
    write_json(batch / "manifest.json", {"runtime": "sensitive"})

    prepared = {
        "account_ids": [7], "evidence": {"action": "created", "created": 1, "failed": 0},
        "mappings": [{
            "auth_path": auth, "auth_file_sha256": MODULE.sha256_file(auth),
            "token_sha256": "token-hash", "refresh_sha256": "refresh-hash", "account_id": 7,
            "log_path": log, "result_path": result,
        }],
        "bundles": [
            {"path": bundle_one, "file": "bundle/sub2api-bundle.json", "sha256": MODULE.sha256_file(bundle_one)},
            {"path": bundle_two, "file": "bundle/pending-sub2api-bundle.json", "sha256": MODULE.sha256_file(bundle_two)},
        ],
        "helper_log": {"path": helper_log, "sha256": MODULE.sha256_file(helper_log)},
        "manifest": {
            "schema_version": 2, "batch_id": batch.name, "status": "completed",
            "summary": {"usable": 1, "usable_exhausted": 0},
            "verification": {}, "backups": [{"file": "backup/pre.dump"}],
            "cleanup": {
                "status": "prepared", "recovery_results_retained": 2,
                "failure_logs_retained": 1,
            },
        },
    }
    result_payload = MODULE.commit_closeout(batch, prepared)
    assert result_payload["status"] == "completed"
    assert not auth.exists()
    assert not log.exists()
    assert result.exists()
    assert failed_result.exists()
    assert failed_log.exists()
    assert backup.exists()
    assert not (batch / "bundle").exists()
    final_manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
    assert "runtime" not in final_manifest
    assert "keep-for-remint" not in (batch / "manifest.json").read_text(encoding="utf-8")
    assert json.loads((batch / "handoff.json").read_text(encoding="utf-8"))["status"] == "completed"


def test_map_sources_requires_exact_static_state():
    source = {
        "token_sha256": "abc", "identity_sha256": "id", "subject_sha256": "sub",
        "refresh_sha256": "refresh", "auth_file_sha256": "file",
    }
    good = {
        7: {
            "id": 7, "token_sha256": "abc", "platform": "grok", "type": "oauth",
            "status": "active", "schedulable": True, "base_url": MODULE.GROK_CLI_BASE_URL,
            "identity_sha256": "id", "subject_sha256": "sub",
            "refresh_sha256": "refresh",
            "proxy_id": 0, "group_count": 1, "bound_grok": True,
        }
    }
    assert MODULE.map_sources_to_accounts([source], good)[0]["account_id"] == 7
    bad = {7: {**good[7], "group_count": 2}}
    with pytest.raises(MODULE.FinalizeError, match="static"):
        MODULE.map_sources_to_accounts([source], bad)
    no_refresh = {7: {**good[7], "refresh_sha256": ""}}
    with pytest.raises(MODULE.FinalizeError, match="refresh"):
        MODULE.map_sources_to_accounts([source], no_refresh)


def test_runtime_state_rejects_nonterminal_attempts():
    manifest = {
        "status": "imported-preprobed", "requested_attempts": 1,
        "successful_registrations": 0, "failed_registrations": 0,
        "attempts": [{"status": "running"}],
    }
    with pytest.raises(MODULE.FinalizeError, match="nonterminal"):
        MODULE.validate_runtime_state(manifest)


def test_live_probe_retries_only_inconclusive_accounts():
    calls = []

    def probe(_config, account_ids):
        calls.append(list(account_ids))
        if len(calls) == 1:
            return [
                {"account_id": 1, "status": 200, "completed": True, "error": ""},
                {"account_id": 2, "status": 200, "completed": False,
                 "error": "429 service temporarily at capacity"},
            ]
        return [{"account_id": 2, "status": 200, "completed": True, "error": ""}]

    result = MODULE.live_probe_classifications({}, [1, 2], probe, attempts=2, retry_delay=0)
    assert result == {1: "usable", 2: "usable"}
    assert calls == [[1, 2], [2]]
