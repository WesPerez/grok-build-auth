from __future__ import annotations

import importlib.util
import base64
import json
import contextlib
import sys
import types
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location("grok2api_reauthorization", Path(__file__).parents[1] / "scripts/reauthorize_grok2api_account.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def batch(tmp_path, *, rows=None, attempts=None):
    root = tmp_path / "runs" / "synthetic-batch"
    results = root / "results"
    results.mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps({"attempts": attempts or [
        {"email": "test@example.invalid", "proxy_ref": "proxy01"}]}))
    (results / "test.json").write_text(json.dumps({"results": rows or [
        {"email": "test@example.invalid", "password": "synthetic-password"}]}))
    return root


def test_material_uses_exact_email_not_first_result(tmp_path):
    batch(tmp_path, rows=[{"email": "unrelated@example.invalid", "password": "wrong"},
                         {"email": "test@example.invalid", "password": "right"}])
    result = module.find_material(tmp_path, {"email": "TEST@example.invalid"})
    assert result["password"] == "right"


def test_multiple_matching_rows_fail_closed(tmp_path):
    batch(tmp_path, rows=[{"email": "test@example.invalid", "password": "a"},
                         {"email": "test@example.invalid", "password": "b"}])
    with pytest.raises(module.RecoveryError, match="result_identity_ambiguous"):
        module.find_material(tmp_path, {"email": "test@example.invalid"})


def test_multiple_batch_matches_fail_closed(tmp_path):
    batch(tmp_path, attempts=[{"email": "test@example.invalid"}, {"email": "test@example.invalid"}])
    with pytest.raises(module.RecoveryError, match="material_ambiguous"):
        module.find_material(tmp_path, {"email": "test@example.invalid"})


def test_result_path_cannot_escape_batch(tmp_path):
    batch(tmp_path, attempts=[{"email": "test@example.invalid", "result_file": str(tmp_path / "outside.json")}])
    with pytest.raises(module.RecoveryError, match="result_outside_batch"):
        module.find_material(tmp_path, {"email": "test@example.invalid"})


@pytest.mark.parametrize("field,value,code", [
    ("email", "other@example.invalid", "new_email_mismatch"),
    ("sub", "another-user", "new_subject_mismatch"),
    ("team_id", "another-team", "new_team_mismatch"),
    ("refresh_token", "", "new_tokens_incomplete"),
])
def test_new_grant_must_match_original_identity(field, value, code):
    target = {"email": "test@example.invalid", "user_id": "user1", "team_id": ""}
    auth = {"type": "xai", "auth_kind": "oauth", "email": target["email"],
            "sub": "user1", "access_token": "synthetic", "refresh_token": "synthetic"}
    module.validate_auth(auth, target)
    auth[field] = value
    with pytest.raises(module.RecoveryError, match=code):
        module.validate_auth(auth, target)


def test_active_account_cannot_be_reauthorized():
    with pytest.raises(module.RecoveryError, match="target_not_revoked"):
        module.require_revoked({"auth_status": "active", "refresh_permanent": 0,
                               "last_refresh_error": ""})


def test_team_identity_uses_token_claims_like_grok2api_import():
    target = {"email": "test@example.invalid", "user_id": "user1", "team_id": "team1"}
    claims = base64.urlsafe_b64encode(json.dumps({"sub": "user1", "team_id": "team1"}).encode()).decode().rstrip("=")
    auth = {"type": "xai", "auth_kind": "oauth", "email": target["email"], "sub": "user1",
            "access_token": "synthetic", "refresh_token": "synthetic", "id_token": "header." + claims + ".signature"}
    module.validate_auth(auth, target)
    del auth["id_token"]
    with pytest.raises(module.RecoveryError, match="new_team_mismatch"):
        module.validate_auth(auth, target)


@pytest.mark.parametrize("message,code", [
    ("CreateSession failed: upstream rejected secret@example.invalid; prior: old consent", "password_session_failed"),
    ("CreateSession failed: Turnstile rejected secret-token", "login_challenge_failed"),
    ("submitOAuth2Consent failed HTTP 403: secret-token", "oauth_consent_failed"),
    ("authorization failed: state mismatch", "oauth_state_mismatch"),
    ("OAuth redirect chain stalled at https://example.invalid/?code=secret-token", "oauth_redirect_incomplete"),
    ("unexpected secret@example.invalid secret-token", "oauth_flow_failed"),
])
def test_authorization_failures_export_only_fixed_reason_codes(message, code):
    assert module.authorization_failure_code(RuntimeError(message)) == code


def test_browser_proxy_preserves_resin_endpoint_and_account():
    assert module.browser_proxy_url("socks5h://AppsGlobal.test:synthetic@127.0.0.1:10834") == "http://AppsGlobal.test:synthetic@127.0.0.1:10834"
    assert module.browser_proxy_url("https://AppsGlobal.test:synthetic@proxy.example.invalid:443") == "https://AppsGlobal.test:synthetic@proxy.example.invalid:443"
    with pytest.raises(module.RecoveryError, match="browser_proxy_invalid"):
        module.browser_proxy_url("socks5h://127.0.0.1:10834")


@pytest.mark.parametrize("method", ["protocol", "browser"])
@pytest.mark.parametrize("execute", [False, True])
def test_cli_captcha_requirement_depends_on_selected_method(monkeypatch, tmp_path, capsys, method, execute):
    from xconsole_client import proxy_pool

    target = {"auth_status": "reauthRequired", "refresh_permanent": 1,
              "last_refresh_error": "invalid_grant", "email": "test@example.invalid",
              "user_id": "user1", "team_id": "",
              "identity_key": module.hashlib.sha256(b"grok_build|user|user1|").hexdigest()}
    monkeypatch.setattr(module, "load_target", lambda *args: target)
    monkeypatch.setattr(module, "find_material", lambda *args: {"proxy_ref": "proxy01"})
    monkeypatch.setattr(module, "load_env", lambda *args: {})
    monkeypatch.setattr(module.os, "umask", lambda mask: 0o077)
    monkeypatch.setattr(proxy_pool, "load_proxy_pool", lambda *args: types.SimpleNamespace(
        schema_version=2, specs=[types.SimpleNamespace(source="resin")],
        url_for=lambda ref: "socks5h://AppsGlobal.test:synthetic@127.0.0.1:10834"))
    calls = []

    def fake_execute(args, current, material, proxy, captcha_key):
        calls.append((args.method, captcha_key))
        return {"status": "exported_identity_verified"}

    monkeypatch.setattr(module, "execute", fake_execute)
    argv = ["reauthorize_grok2api_account.py", "--account-id", "1",
            "--private-dir", str(tmp_path), "--method", method]
    if execute:
        argv += ["--execute", "--expected-identity", target["identity_key"],
                 "--backup", str(tmp_path / "backup.db"),
                 "--output-dir", str(tmp_path / "runs" / "recovery")]
    monkeypatch.setattr(sys, "argv", argv)
    assert module.main() == (0 if method == "browser" else 1)
    summary = json.loads(capsys.readouterr().out)
    if method == "protocol":
        assert summary["reason"] == "captcha_key_missing"
        assert calls == []
    else:
        assert summary["status"] == ("exported_identity_verified" if execute else "plan_ready")
        assert calls == ([("browser", "")] if execute else [])


def test_browser_recovery_owns_a_new_private_profile(monkeypatch, tmp_path):
    from xconsole_client import xai_oauth, registration_backends

    calls = []
    profile = tmp_path / "private" / "browser-profile"
    page = types.SimpleNamespace(goto=lambda *args, **kwargs: None)
    browser = types.SimpleNamespace(new_page=lambda: page, close=lambda: calls.append("closed"))

    def launch_persistent(path, **kwargs):
        assert Path(path) == profile
        assert profile.stat().st_mode & 0o077 == 0
        assert kwargs["headless"] is True
        assert kwargs["proxy"]["server"] == "http://127.0.0.1:10834"
        assert kwargs["proxy"]["username"] == "AppsGlobal.test"
        calls.append("launched")
        return browser

    api = types.ModuleType("playwright.sync_api")
    api.sync_playwright = lambda: contextlib.nullcontext(types.SimpleNamespace(
        chromium=types.SimpleNamespace(launch_persistent_context=launch_persistent)))
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", api)
    monkeypatch.setattr(registration_backends, "_edge_executable", lambda: "/synthetic/edge")
    server = types.SimpleNamespace(shutdown=lambda: None, server_close=lambda: None)
    sink = types.SimpleNamespace(event=types.SimpleNamespace(is_set=lambda: True))
    monkeypatch.setattr(xai_oauth, "_start_pkce_callback_server", lambda **kwargs: (
        server, sink, "https://accounts.x.ai/authorize", "http://127.0.0.1/callback", "state", "verifier"))
    monkeypatch.setattr(xai_oauth, "_wait_oauth_code", lambda *args: "synthetic-code")
    sentinel = object()
    monkeypatch.setattr(xai_oauth, "_finalize_oauth_code", lambda **kwargs: sentinel)
    kwargs = {"proxy": "http://AppsGlobal.test:synthetic@127.0.0.1:10834", "browser_profile_dir": profile}
    assert xai_oauth.login_with_playwright("test@example.invalid", "synthetic", **kwargs) is sentinel
    assert calls == ["launched", "closed"]
    with pytest.raises(FileExistsError):
        xai_oauth.login_with_playwright("test@example.invalid", "synthetic", **kwargs)
    assert calls == ["launched", "closed"]
