import importlib.util
import json
from pathlib import Path
import datetime as dt
from types import SimpleNamespace

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
            "identity_sha256": "identity-hash", "subject_sha256": "subject-hash",
            "log_path": log, "log_sha256": MODULE.sha256_file(log), "result_path": result,
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
    checkpoint = json.loads((batch / "import" / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["sources"][0]["account_id"] == 7
    journal = json.loads((batch / "import" / "cleanup-journal.json").read_text(encoding="utf-8"))
    assert journal["status"] == "completed"
    assert {item["status"] for item in journal["entries"]} == {"deleted"}


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
            "global_identity_count": 1,
        }
    }
    assert MODULE.map_sources_to_accounts([source], good)[0]["account_id"] == 7
    bad = {7: {**good[7], "group_count": 2}}
    with pytest.raises(MODULE.FinalizeError, match="static"):
        MODULE.map_sources_to_accounts([source], bad)
    no_refresh = {7: {**good[7], "refresh_sha256": ""}}
    with pytest.raises(MODULE.FinalizeError, match="refresh"):
        MODULE.map_sources_to_accounts([source], no_refresh)
    duplicate_identity = {7: {**good[7], "global_identity_count": 2}}
    with pytest.raises(MODULE.FinalizeError, match="static"):
        MODULE.map_sources_to_accounts([source], duplicate_identity)


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
    assert {key: value["classification"] for key, value in result.items()} == {
        1: "usable", 2: "usable",
    }
    assert calls == [[1, 2], [2]]


def test_probe_evidence_is_structured_and_does_not_retain_error_text():
    evidence = MODULE.classify_probe_evidence({
        "account_id": 7, "status": 200, "completed": False,
        "error": "429 subscription:free-usage-exhausted rolling 24-hour window secret-detail",
    })
    assert evidence["classification"] == "usable_exhausted"
    assert evidence["code"] == "RATE_LIMITED"
    assert evidence["status"] == 429
    assert "error" not in evidence
    assert evidence["observed_at"].endswith("Z")
    structured = MODULE.classify_probe_evidence({
        "account_id": 8, "status": 200, "completed": False,
        "availability": "usable_exhausted", "code": "RATE_LIMITED",
    })
    assert structured["classification"] == "usable_exhausted"
    assert structured["status"] == 200


def test_prepared_artifact_rejects_tamper_and_expiry(tmp_path):
    batch = tmp_path / "20260714T120000Z-abcdef"
    (batch / "import").mkdir(parents=True)
    artifact = {
        "schema_version": 1, "batch_id": batch.name,
        "status": "live-verification-prepared",
        "prepared_at": MODULE.utc_now(),
        "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "account_ids": [], "manifest": {}, "evidence": {}, "mappings": [],
        "bundles": [], "helper_log": {"path": "import/helper.log", "sha256": "x"},
    }
    artifact["artifact_sha256"] = MODULE._canonical_sha256(artifact)
    write_json(batch / "import" / MODULE.PREPARED_ARTIFACT, artifact)
    loaded = MODULE.load_prepared_artifact(batch)
    assert loaded["batch_id"] == batch.name

    artifact["account_ids"] = [99]
    write_json(batch / "import" / MODULE.PREPARED_ARTIFACT, artifact)
    with pytest.raises(MODULE.FinalizeError, match="hash mismatch"):
        MODULE.load_prepared_artifact(batch)

    artifact["account_ids"] = []
    artifact["expires_at"] = "2000-01-01T00:00:00Z"
    artifact.pop("artifact_sha256", None)
    artifact["artifact_sha256"] = MODULE._canonical_sha256(artifact)
    write_json(batch / "import" / MODULE.PREPARED_ARTIFACT, artifact)
    with pytest.raises(MODULE.FinalizeError, match="expired"):
        MODULE.load_prepared_artifact(batch)


def test_cleanup_resumes_from_quarantined_entry(tmp_path):
    batch = tmp_path / "20260714T120000Z-abcdef"
    source = batch / "auth" / "x" / "auth.json"
    write_json(source, {"token": "secret"})
    quarantine = batch / "cleanup" / "quarantine" / "0000-auth.json"
    quarantine.parent.mkdir(parents=True)
    source_hash = MODULE.sha256_file(source)
    source.replace(quarantine)
    journal_path = batch / "import" / "cleanup-journal.json"
    journal = {
        "schema_version": 1, "batch_id": batch.name, "status": "quarantining",
        "account_ids": [7], "entries": [{
            "kind": "oauth_source", "source": "auth/x/auth.json",
            "quarantine": "cleanup/quarantine/0000-auth.json",
            "sha256": source_hash, "status": "pending",
        }],
    }
    write_json(journal_path, journal)
    MODULE._quarantine_entries(batch, journal_path, journal)
    assert journal["entries"][0]["status"] == "quarantined"
    MODULE._purge_quarantine(batch, journal_path, journal)
    assert journal["status"] == "completed"
    assert not quarantine.exists()


def test_backup_records_preserve_import_and_reconcile_roles(tmp_path):
    batch = tmp_path / "20260714T120000Z-abcdef"
    backup_dir = batch / "backup"
    backup_dir.mkdir(parents=True)
    pre_import = backup_dir / "pre-import.dump"
    pre_reconcile = backup_dir / "pre-reconcile.dump"
    for path, data in ((pre_import, b"import"), (pre_reconcile, b"reconcile")):
        path.write_bytes(data)
        path.chmod(0o600)
    helper = {
        "path": str(pre_import), "bytes": pre_import.stat().st_size,
        "sha256": MODULE.sha256_file(pre_import),
    }
    manifest = {"backup_history": [
        helper,
        {
            "path": str(pre_reconcile), "bytes": pre_reconcile.stat().st_size,
            "sha256": MODULE.sha256_file(pre_reconcile),
        },
    ]}
    records = MODULE.backup_records(
        batch, {}, helper, manifest, verifier=lambda _config, _path: None,
    )
    assert {item["kind"] for item in records} == {
        "pre_sub2api_import", "pre_grok_reconcile",
    }
    assert all(item["retention_status"] == "retain_until_explicit_backup_policy" for item in records)
    assert all(item["pg_restore_list_verified_at"].endswith("Z") for item in records)


def test_cleanup_mode_does_not_load_config_or_run_live_verification(tmp_path, monkeypatch, capsys):
    private = tmp_path / "private"
    batch = private / "runs" / "20260714T120000Z-abcdef"
    batch.mkdir(parents=True)
    write_json(batch / "manifest.json", {})
    monkeypatch.setattr(MODULE, "parse_args", lambda: SimpleNamespace(
        batch=batch.name, private_dir=str(private),
        prepare_live_verification=False, confirm_cleanup=True,
    ))
    monkeypatch.setattr(MODULE, "acquire_non_destructive_batch_lock", lambda _private: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(MODULE, "load_env", lambda _path: pytest.fail("cleanup loaded runtime config"))
    monkeypatch.setattr(MODULE, "prepare_closeout", lambda *_args, **_kwargs: pytest.fail("cleanup ran live verification"))
    monkeypatch.setattr(MODULE, "load_prepared_artifact", lambda *_args, **_kwargs: {"account_ids": [7]})
    monkeypatch.setattr(MODULE, "commit_closeout", lambda _batch, _prepared: {
        "batch_id": batch.name, "status": "completed",
    })
    assert MODULE.main() == 0
    assert '"status": "completed"' in capsys.readouterr().out
