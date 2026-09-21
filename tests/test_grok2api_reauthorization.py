from __future__ import annotations

import importlib.util
import base64
import json
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
