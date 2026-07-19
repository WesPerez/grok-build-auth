import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "register_and_import.py"
SPEC = importlib.util.spec_from_file_location("register_and_import", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

WEB_MODULE_PATH = Path(__file__).resolve().parents[1] / "web_console.py"
WEB_SPEC = importlib.util.spec_from_file_location("web_console_test", WEB_MODULE_PATH)
assert WEB_SPEC and WEB_SPEC.loader
WEB_MODULE = importlib.util.module_from_spec(WEB_SPEC)
WEB_SPEC.loader.exec_module(WEB_MODULE)


def write_auth(path: Path, email: str = "xaiabcdef@example.com") -> Path:
    path.write_text(json.dumps({
        "email": email,
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "base_url": "https://cli-chat-proxy.grok.com/v1",
    }), encoding="utf-8")
    return path


def test_build_bundle_uses_current_timestamp_and_token_hash(tmp_path):
    base_url = "https://cli-chat-proxy.grok.com/v1"
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


def test_batch_lock_rejects_second_process_lock(tmp_path):
    first = MODULE.acquire_batch_lock(tmp_path / "batch.lock")
    try:
        with pytest.raises(MODULE.BatchError, match="already running"):
            MODULE.acquire_batch_lock(tmp_path / "batch.lock")
    finally:
        first.close()


def test_proxy_ids_are_validated_before_registration(monkeypatch):
    pool = SimpleNamespace(
        configured=True,
        specs=(SimpleNamespace(sub2api_proxy_id=12), SimpleNamespace(sub2api_proxy_id=13)),
    )
    monkeypatch.setattr(MODULE, "run", lambda command: SimpleNamespace(returncode=0, stdout="12\n"))
    with pytest.raises(MODULE.BatchError, match="13"):
        MODULE.validate_sub2api_proxy_ids({
            "SUB2API_POSTGRES_CONTAINER": "postgres",
            "SUB2API_PG_USER": "sub2api",
            "SUB2API_PG_DB": "sub2api",
        }, pool)


def test_resume_bundle_requires_matching_hash_and_exact_path(tmp_path):
    batch = tmp_path / "runs" / "batch-1"
    auth_dir = batch / "auth" / "xaiabcdef"
    bundle_dir = batch / "bundle"
    auth_dir.mkdir(parents=True)
    bundle_dir.mkdir()
    auth = write_auth(auth_dir / "xaiabcdef@example.com.json")
    bundle = bundle_dir / "sub2api-bundle.json"
    MODULE.atomic_json(bundle, MODULE.build_bundle([auth], "https://cli-chat-proxy.grok.com/v1"))
    MODULE.atomic_json(batch / "manifest.json", {
        "batch_id": "batch-1", "status": "import-failed", "bundle": str(bundle),
        "bundle_sha256": MODULE.hashlib.sha256(bundle.read_bytes()).hexdigest(),
        "attempts": [{"status": "registered", "auth_file": str(auth)}],
    })
    manifest, loaded_bundle, auth_paths = MODULE.load_resume_bundle(batch)
    assert manifest["batch_id"] == "batch-1"
    assert loaded_bundle == bundle.resolve()
    assert auth_paths == [auth.resolve()]


def test_grok_target_config_requires_dedicated_group_and_official_cli_url():
    assert MODULE.validate_grok_target_config({
        "SUB2API_GROUP": "grok",
        "GROK_ACCOUNT_BASE_URL": "https://cli-chat-proxy.grok.com/v1",
    }) == "https://cli-chat-proxy.grok.com/v1"
    with pytest.raises(MODULE.BatchError, match="must be grok"):
        MODULE.validate_grok_target_config({
            "SUB2API_GROUP": "openai",
            "GROK_ACCOUNT_BASE_URL": "https://cli-chat-proxy.grok.com/v1",
        })
    with pytest.raises(MODULE.BatchError, match="cli-chat-proxy"):
        MODULE.validate_grok_target_config({
            "SUB2API_GROUP": "grok",
            "GROK_ACCOUNT_BASE_URL": "https://api.x.ai/v1",
        })


def test_import_command_carries_ops_environment_and_write_confirmations(tmp_path):
    command = MODULE.build_sub2api_import_command({
        "SUB2API_IMPORT_TOOL": "/skills/k12-sub2api-ops/scripts/sub2api_live_tool.py",
        "SUB2API_POSTGRES_CONTAINER": "sub2api-prod-postgres",
        "SUB2API_PG_USER": "sub2api",
        "SUB2API_PG_DB": "sub2api",
        "SUB2API_ENVIRONMENT": "production",
        "SUB2API_ENV": "/srv/sub2api/.env",
        "SUB2API_URL": "http://127.0.0.1:13080",
        "SUB2API_GROUP": "grok",
    }, bundle_path=tmp_path / "bundle.json", backup_dir=tmp_path / "backup",
       confirm_production_write=True)

    assert command[command.index("--environment") + 1] == "production"
    assert "--confirm-write" in command
    assert "--confirm-production-write" in command


def test_production_import_command_rejects_missing_confirmation(tmp_path):
    config = {
        "SUB2API_IMPORT_TOOL": "/skills/k12-sub2api-ops/scripts/sub2api_live_tool.py",
        "SUB2API_POSTGRES_CONTAINER": "sub2api-prod-postgres",
        "SUB2API_PG_USER": "sub2api",
        "SUB2API_PG_DB": "sub2api",
        "SUB2API_ENVIRONMENT": "production",
        "SUB2API_ENV": "/srv/sub2api/.env",
        "SUB2API_URL": "http://127.0.0.1:13080",
        "SUB2API_GROUP": "grok",
    }
    with pytest.raises(MODULE.BatchError, match="confirm-production-write"):
        MODULE.build_sub2api_import_command(
            config,
            bundle_path=tmp_path / "bundle.json",
            backup_dir=tmp_path / "backup",
            confirm_production_write=False,
        )


def test_reconcile_updates_exact_ids_through_admin_api(monkeypatch):
    updated = []
    monkeypatch.setattr(MODULE, "grok_group_id", lambda config: 5)
    monkeypatch.setattr(MODULE, "make_admin_token", lambda config: "short-lived-token")
    monkeypatch.setattr(
        MODULE, "update_account_via_admin_api",
        lambda config, token, account_id, group_id, proxy_id=None: updated.append((token, account_id, group_id, proxy_id)),
    )
    MODULE.reconcile_imported_accounts({
        "SUB2API_POSTGRES_CONTAINER": "postgres",
        "SUB2API_PG_USER": "user",
        "SUB2API_PG_DB": "db",
        "SUB2API_GROUP": "grok",
        "GROK_ACCOUNT_BASE_URL": "https://cli-chat-proxy.grok.com/v1",
    }, [17, 23])
    assert updated == [("short-lived-token", 17, 5, 0), ("short-lived-token", 23, 5, 0)]


def test_reconcile_clears_existing_proxy_binding_by_default(monkeypatch):
    updated = []
    monkeypatch.setattr(MODULE, "grok_group_id", lambda config: 5)
    monkeypatch.setattr(MODULE, "make_admin_token", lambda config: "short-lived-token")
    monkeypatch.setattr(
        MODULE, "update_account_via_admin_api",
        lambda config, token, account_id, group_id, proxy_id=None: updated.append(proxy_id),
    )

    MODULE.reconcile_imported_accounts({}, [17])

    assert updated == [0]


def test_build_bundle_rejects_duplicate_auth_tokens(tmp_path):
    first = write_auth(tmp_path / "one.json", "xai111111@example.com")
    second = write_auth(tmp_path / "two.json", "xai222222@example.com")
    with pytest.raises(MODULE.BatchError, match="duplicate"):
        MODULE.build_bundle([first, second], "https://cli-chat-proxy.grok.com/v1")


def test_manifest_stage_records_activity_and_stage_start(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    attempt = {"stage": "mailbox"}
    manifest = {"current_stage": "registration", "attempts": [attempt]}
    MODULE.set_manifest_stage(manifest, manifest_path, "signup", attempt=attempt)
    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert saved["attempts"][0]["stage"] == "signup"
    assert saved["attempts"][0]["stage_started_at"]
    assert saved["attempts"][0]["last_activity_at"]
    assert saved["last_activity_at"]
    assert saved["updated_at"]


def test_run_emits_heartbeat_while_child_is_quiet():
    beats = []
    proc = MODULE.run(
        [MODULE.sys.executable, "-c", "import time; time.sleep(0.08); print('done')"],
        on_line=lambda line: None,
        on_heartbeat=lambda: beats.append(True),
        heartbeat_interval=0.02,
    )
    assert proc.returncode == 0
    assert beats


def test_doctor_cache_avoids_repeated_probes(monkeypatch):
    calls = []
    monkeypatch.setattr(WEB_MODULE, "_DOCTOR_CACHE", (0.0, []))
    monkeypatch.setattr(WEB_MODULE, "doctor", lambda: calls.append(True) or [{"name": "ok", "ok": True}])
    assert WEB_MODULE.cached_doctor() == [{"name": "ok", "ok": True}]
    assert WEB_MODULE.cached_doctor() == [{"name": "ok", "ok": True}]
    assert len(calls) == 1
    WEB_MODULE.cached_doctor(force=True)
    assert len(calls) == 2


def test_web_console_validates_sub2api_environment():
    assert WEB_MODULE.valid_sub2api_environment("production")
    assert WEB_MODULE.valid_sub2api_environment("test")
    assert not WEB_MODULE.valid_sub2api_environment("")
    assert not WEB_MODULE.valid_sub2api_environment("prod")


def test_state_marks_active_batch_interrupted_without_process(monkeypatch):
    monkeypatch.setattr(WEB_MODULE.MANAGER, "current", lambda: None)
    monkeypatch.setattr(WEB_MODULE, "list_batches", lambda: [{"batch_id": "batch-1", "status": "running"}])
    monkeypatch.setattr(WEB_MODULE, "cached_doctor", lambda: [])
    payload = WEB_MODULE.state_payload()
    assert payload["batches"][0]["runtime_state"] == "interrupted"
    assert payload["batches"][0]["action_hint"]


def test_task_manager_refuses_second_task_after_web_restart(monkeypatch):
    manager = WEB_MODULE.TaskManager()
    manager.process = None
    manager.task = {"running": True, "pid": 123, "process_started_ticks": "7"}
    monkeypatch.setattr(manager, "_restored_process_alive", lambda: True)
    with pytest.raises(RuntimeError, match="restored task"):
        manager.start(["true"], "test", "test")


def test_preimport_auth_probes_require_every_account_http_200(tmp_path, monkeypatch):
    paths = [write_auth(tmp_path / "one.json"), write_auth(tmp_path / "two.json", "xai222222@example.com")]
    responses = iter([{"status": 200}, {"status": 403}])
    monkeypatch.setattr(MODULE, "probe_grok_auth", lambda path, timeout, proxy="": next(responses))
    result = MODULE.run_preimport_auth_probes(paths, timeout=1)
    assert result["tested"] == 2
    assert result["http_200_completed"] == 1
    assert result["passed"] is False


def test_preimport_auth_probes_retry_transient_forbidden_until_success(tmp_path, monkeypatch):
    path = write_auth(tmp_path / "one.json")
    responses = iter([{"status": 403}, {"status": 403}, {"status": 200}])
    clock = [0.0]

    monkeypatch.setattr(MODULE, "probe_grok_auth", lambda auth_path, timeout, proxy="": next(responses))

    result = MODULE.run_preimport_auth_probes(
        [path],
        timeout=1,
        max_wait_seconds=120,
        retry_interval_seconds=60,
        sleep_fn=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        monotonic_fn=lambda: clock[0],
    )

    assert result["passed"] is True
    assert result["http_200_completed"] == 1
    assert result["elapsed_seconds"] == 120
    assert result["results"][0]["attempts"] == 3


def test_preimport_auth_probes_do_not_retry_rate_limit(tmp_path, monkeypatch):
    path = write_auth(tmp_path / "one.json")
    calls = []
    monkeypatch.setattr(
        MODULE,
        "probe_grok_auth",
        lambda auth_path, timeout, proxy="": calls.append(auth_path) or {"status": 429},
    )

    result = MODULE.run_preimport_auth_probes(
        [path],
        timeout=1,
        max_wait_seconds=900,
        sleep_fn=lambda seconds: (_ for _ in ()).throw(AssertionError("must not sleep")),
    )

    assert result["passed"] is False
    assert result["results"][0]["attempts"] == 1
    assert len(calls) == 1


def test_preprobe_proxy_map_falls_back_when_original_node_is_unhealthy(tmp_path):
    auth = write_auth(tmp_path / "one.json")
    proxy_config = tmp_path / "proxies.json"
    proxy_config.write_text(json.dumps({
        "version": 1,
        "proxies": [{"ref": "node-b", "url_env": "NODE_B"}],
    }), encoding="utf-8")
    proxy_config.chmod(0o600)
    healthy_pool = MODULE.load_proxy_pool(str(proxy_config), {
        "NODE_B": "socks5://127.0.0.1:10901",
        "GROK_BIND_SUB2API_PROXY_AFTER_IMPORT": "false",
    })
    attempts = [{"auth_file": str(auth), "proxy_ref": "node-a"}]

    mapping = MODULE.build_preprobe_proxy_map([auth], attempts, healthy_pool)

    assert mapping == {auth.name: "socks5://127.0.0.1:10901"}
    assert attempts[0]["preprobe_proxy_fallback_from"] == "node-a"
    assert attempts[0]["preprobe_proxy_ref"] == "node-b"


def test_group_probe_key_uses_active_target_group_key(monkeypatch):
    commands = []
    monkeypatch.setattr(
        MODULE,
        "run",
        lambda command: commands.append(command) or MODULE.subprocess.CompletedProcess(command, 0, "sk-probe\n", None),
    )
    config = {
        "SUB2API_GROUP": "grok",
        "SUB2API_POSTGRES_CONTAINER": "postgres",
        "SUB2API_PG_USER": "user",
        "SUB2API_PG_DB": "db",
    }
    assert MODULE.resolve_group_probe_key(config) == "sk-probe"
    assert "g.name='grok'" in commands[0][-1]


def test_postimport_account_probes_require_each_sse_completion(monkeypatch):
    bodies = iter([
        b'data: {"type":"test_complete","success":true}\n\n',
        b'data: {"type":"error","error":"upstream failed"}\n\n',
    ])

    class Response:
        status = 200

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            return self.body

    class Opener:
        def open(self, request, timeout):
            return Response(next(bodies))

    monkeypatch.setattr(MODULE, "make_admin_token", lambda config: "admin-jwt")
    monkeypatch.setattr(MODULE.urllib.request, "build_opener", lambda *handlers: Opener())
    result = MODULE.run_postimport_account_probes({"SUB2API_URL": "http://sub2api"}, [11, 12])
    assert result["tested"] == 2
    assert result["passed"] is False
    assert result["usable_count"] == 1
    assert result["usable_exhausted_count"] == 0
    assert result["failed_count"] == 1
    assert result["results"][0]["completed"] is True
    assert result["results"][0]["availability"] == "usable"
    assert result["results"][1]["error"] == "upstream failed"
    assert result["results"][1]["availability"] == "unknown_error"


def test_postimport_account_probes_accept_sse_quota_as_usable_exhausted(monkeypatch):
    body = (
        b'data: {"type":"error","error":"Grok Responses API returned 429: '
        b'subscription:free-usage-exhausted rolling 24-hour limit"}\n\n'
    )

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            return body

    class Opener:
        def open(self, request, timeout):
            return Response()

    monkeypatch.setattr(MODULE, "make_admin_token", lambda config: "admin-jwt")
    monkeypatch.setattr(MODULE.urllib.request, "build_opener", lambda *handlers: Opener())
    result = MODULE.run_postimport_account_probes({"SUB2API_URL": "http://sub2api"}, [101048])

    assert result["passed"] is True
    assert result["usable_count"] == 0
    assert result["usable_exhausted_count"] == 1
    assert result["failed_count"] == 0
    assert result["results"][0]["availability"] == "usable_exhausted"
    assert result["results"][0]["code"] == "RATE_LIMITED"
    assert result["results"][0]["quota_evidence"] == {
        "status": 429, "reason": "FREE_USAGE_EXHAUSTED",
    }


@pytest.mark.parametrize("status", [402, 429])
def test_postimport_account_probes_accept_outer_quota_status(monkeypatch, status):
    class Opener:
        def open(self, request, timeout):
            raise MODULE.urllib.error.HTTPError(
                request.full_url, status, "quota", {}, io.BytesIO(b'{"code":"quota exhausted"}'),
            )

    monkeypatch.setattr(MODULE, "make_admin_token", lambda config: "admin-jwt")
    monkeypatch.setattr(MODULE.urllib.request, "build_opener", lambda *handlers: Opener())
    result = MODULE.run_postimport_account_probes({"SUB2API_URL": "http://sub2api"}, [12])

    assert result["passed"] is True
    assert result["usable_exhausted_count"] == 1
    assert result["results"][0]["status"] == status
    assert result["results"][0]["code"] == "RATE_LIMITED"
    assert result["results"][0]["quota_evidence"] == {
        "status": status, "reason": "QUOTA_EXHAUSTED",
    }


@pytest.mark.parametrize("body", [
    "service temporarily at capacity; retry shortly",
    "resource has been exhausted",
    "rate limit exceeded",
])
def test_postimport_account_probe_rejects_ordinary_429(body):
    assert MODULE.classify_postimport_account_probe(429, body, False) == (
        "unknown_error", "HTTP_429",
    )
    assert MODULE.explicit_quota_evidence(429, body) is None


def test_postimport_account_probe_requires_402_or_429_quota_status():
    body = "subscription:free-usage-exhausted rolling 24-hour window"
    assert MODULE.classify_postimport_account_probe(200, body, False) == (
        "unknown_error", "HTTP_200",
    )
    assert MODULE.explicit_quota_evidence(200, body) is None


def test_record_manifest_backup_preserves_history_and_deduplicates(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest = {
        "batch_id": "batch",
        "backup_history": [{"path": "old.dump", "bytes": 1, "sha256": "old"}],
    }
    backup = {"path": "new.dump", "bytes": 2, "sha256": "new"}

    MODULE.record_manifest_backup(
        manifest, manifest_path, backup,
        kind="pre_grok_reconcile", source="test",
    )
    MODULE.record_manifest_backup(
        manifest, manifest_path, backup,
        kind="pre_grok_reconcile", source="test",
    )

    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert saved["backup"]["kind"] == "pre_grok_reconcile"
    assert saved["backup"]["retention_status"] == "retain_until_batch_finalized"
    assert saved["backup"]["deleted_at"] is None
    assert [item["sha256"] for item in saved["backup_history"]] == ["new", "old"]


def test_proxy_pool_is_direct_when_not_configured():
    pool = MODULE.load_proxy_pool("", {})
    assert pool.configured is False
    assert pool.acquire() is None


def test_proxy_pool_leases_refs_without_exposing_urls(tmp_path):
    config = tmp_path / "proxies.json"
    config.write_text(json.dumps({
        "version": 1,
        "proxies": [
            {"ref": "node-a", "url_env": "NODE_A", "max_active_leases": 1, "sub2api_proxy_id": 11},
            {"ref": "node-b", "url_env": "NODE_B", "max_active_leases": 1, "sub2api_proxy_id": 12},
        ],
    }), encoding="utf-8")
    config.chmod(0o600)
    pool = MODULE.load_proxy_pool(str(config), {
        "NODE_A": "http://user:secret@proxy-a.example:8080",
        "NODE_B": "socks5h://user:secret@proxy-b.example:1080",
    })
    first = pool.acquire()
    second = pool.acquire()
    assert {first.ref, second.ref} == {"node-a", "node-b"}
    with pytest.raises(MODULE.ProxyPoolError, match="no available"):
        pool.acquire()
    pool.release(first)
    assert pool.acquire().ref == first.ref


def test_proxy_pool_configured_empty_fails_closed(tmp_path):
    config = tmp_path / "proxies.json"
    config.write_text(json.dumps({"version": 1, "proxies": []}), encoding="utf-8")
    config.chmod(0o600)
    pool = MODULE.load_proxy_pool(str(config), {})
    with pytest.raises(MODULE.ProxyPoolError, match="no enabled nodes"):
        pool.acquire()


def test_proxy_pool_rejects_group_readable_secret_file(tmp_path):
    config = tmp_path / "proxies.json"
    config.write_text(json.dumps({"version": 1, "proxies": []}), encoding="utf-8")
    config.chmod(0o640)
    with pytest.raises(MODULE.ProxyPoolError, match="0600"):
        MODULE.load_proxy_pool(str(config), {})


def test_proxy_pool_rejects_unsafe_ref_and_url(tmp_path):
    config = tmp_path / "proxies.json"
    config.write_text(json.dumps({
        "version": 1,
        "proxies": [{"ref": "node\nsecret", "url_env": "NODE"}],
    }), encoding="utf-8")
    config.chmod(0o600)
    with pytest.raises(MODULE.ProxyPoolError, match="unique ref"):
        MODULE.load_proxy_pool(str(config), {"NODE": "file:///etc/passwd"})

    config.write_text(json.dumps({
        "version": 1,
        "proxies": [{"ref": "node-safe", "url_env": "NODE"}],
    }), encoding="utf-8")
    with pytest.raises(MODULE.ProxyPoolError, match="valid URL"):
        MODULE.load_proxy_pool(str(config), {"NODE": "file:///etc/passwd"})


def test_proxy_pool_requires_sub2api_proxy_id_by_default(tmp_path):
    config = tmp_path / "proxies.json"
    config.write_text(json.dumps({
        "version": 1,
        "proxies": [{"ref": "node-safe", "url_env": "NODE"}],
    }), encoding="utf-8")
    config.chmod(0o600)

    with pytest.raises(MODULE.ProxyPoolError, match="post-import stickiness"):
        MODULE.load_proxy_pool(str(config), {
            "NODE": "socks5://127.0.0.1:10900",
            "GROK_BIND_SUB2API_PROXY_AFTER_IMPORT": "true",
        })


def test_proxy_pool_allows_missing_sub2api_proxy_id_only_with_explicit_flag(tmp_path):
    config = tmp_path / "proxies.json"
    config.write_text(json.dumps({
        "version": 1,
        "proxies": [{"ref": "node-safe", "url_env": "NODE"}],
    }), encoding="utf-8")
    config.chmod(0o600)

    pool = MODULE.load_proxy_pool(str(config), {
        "NODE": "socks5://127.0.0.1:10900",
        "GROK_BIND_SUB2API_PROXY_AFTER_IMPORT": "true",
        "GROK_ALLOW_MISSING_SUB2API_PROXY_IDS": "true",
    })

    assert pool.configured is True
    assert pool.specs[0].sub2api_proxy_id is None


def test_proxy_pool_registration_only_mode_allows_missing_proxy_id(tmp_path):
    config = tmp_path / "proxies.json"
    config.write_text(json.dumps({
        "version": 1,
        "proxies": [{"ref": "node-safe", "url_env": "NODE"}],
    }), encoding="utf-8")
    config.chmod(0o600)
    pool = MODULE.load_proxy_pool(str(config), {
        "NODE": "socks5://127.0.0.1:10900",
        "GROK_BIND_SUB2API_PROXY_AFTER_IMPORT": "false",
    })
    assert pool.specs[0].sub2api_proxy_id is None


def test_proxy_pool_rotation_persists_across_process_instances(tmp_path):
    tmp_path.chmod(0o700)
    config = tmp_path / "proxies.json"
    state = tmp_path / "proxy-rotation.json"
    config.write_text(json.dumps({
        "version": 1,
        "proxies": [
            {"ref": "node-a", "url_env": "NODE_A", "sub2api_proxy_id": 11},
            {"ref": "node-b", "url_env": "NODE_B", "sub2api_proxy_id": 12},
        ],
    }), encoding="utf-8")
    config.chmod(0o600)
    values = {
        "NODE_A": "socks5://127.0.0.1:10900",
        "NODE_B": "socks5://127.0.0.1:10901",
        "GROK_PROXY_ROTATION_STATE_FILE": str(state),
    }

    first_pool = MODULE.load_proxy_pool(str(config), values)
    first = first_pool.acquire()
    assert first.ref == "node-a"
    first_pool.release(first)

    second_pool = MODULE.load_proxy_pool(str(config), values)
    second = second_pool.acquire()
    assert second.ref == "node-b"
    second_pool.release(second)

    third_pool = MODULE.load_proxy_pool(str(config), values)
    assert third_pool.acquire().ref == "node-a"
    assert state.stat().st_mode & 0o077 == 0


def test_proxy_pool_rotation_write_failure_does_not_leak_lease(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    config = tmp_path / "proxies.json"
    state = tmp_path / "proxy-rotation.json"
    config.write_text(json.dumps({
        "version": 1,
        "proxies": [{"ref": "node-a", "url_env": "NODE_A", "sub2api_proxy_id": 11}],
    }), encoding="utf-8")
    config.chmod(0o600)
    pool = MODULE.load_proxy_pool(str(config), {
        "NODE_A": "socks5://127.0.0.1:10900",
        "GROK_PROXY_ROTATION_STATE_FILE": str(state),
    })
    monkeypatch.setattr(pool, "_write_rotation_cursor", lambda cursor: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        pool.acquire()
    monkeypatch.undo()
    lease = pool.acquire()
    assert lease.ref == "node-a"


def test_proxy_pool_rejects_rotation_state_path_collisions(tmp_path):
    tmp_path.chmod(0o700)
    config = tmp_path / "proxies.json"
    config.write_text(json.dumps({
        "version": 1,
        "proxies": [{"ref": "node-a", "url_env": "NODE_A", "sub2api_proxy_id": 11}],
    }), encoding="utf-8")
    config.chmod(0o600)
    values = {
        "NODE_A": "socks5://127.0.0.1:10900",
        "GROK_PROXY_ROTATION_STATE_FILE": str(config),
    }
    with pytest.raises(MODULE.ProxyPoolError, match="must differ"):
        MODULE.load_proxy_pool(str(config), values)

    outside = tmp_path.parent / "outside-rotation.json"
    values["GROK_PROXY_ROTATION_STATE_FILE"] = str(outside)
    with pytest.raises(MODULE.ProxyPoolError, match="beside"):
        MODULE.load_proxy_pool(str(config), values)

    target = tmp_path / "real-rotation.json"
    target.write_text('{"version":1,"next_ref":"node-a"}\n', encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "linked-rotation.json"
    link.symlink_to(target)
    values["GROK_PROXY_ROTATION_STATE_FILE"] = str(link)
    with pytest.raises(MODULE.ProxyPoolError, match="symlink"):
        MODULE.load_proxy_pool(str(config), values)
