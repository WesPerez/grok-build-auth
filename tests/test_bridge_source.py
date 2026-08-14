from __future__ import annotations

import importlib.util
import base64
import io
import json
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridge" / "bridge.py"


def jwt_token(iat, exp, sub="subject"):
    def segment(value):
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{segment({'alg': 'none'})}.{segment({'iat': iat, 'exp': exp, 'sub': sub})}.{'x' * 64}"


def load_bridge(monkeypatch, tmp_path):
    for name in ("mailu", "sub2api", "management", "jwt"):
        path = tmp_path / name
        path.write_text(f"{name}-secret", encoding="utf-8")
        path.chmod(0o600)
        monkeypatch.setenv(f"{name.upper()}_TOKEN_FILE", str(path))
    monkeypatch.setenv("MAILU_API_TOKEN_FILE", str(tmp_path / "mailu"))
    monkeypatch.setenv("MAILU_API_BASE", "https://mail.example/api")
    monkeypatch.setenv("MAILU_DOMAIN", "example.com")
    monkeypatch.setenv("MAILU_DOMAINS", "example.com,alt.example.com")
    monkeypatch.setenv("MAILU_IMAP_HOST", "mail.example.com")
    monkeypatch.setenv("SUB2API_ADMIN_KEY_FILE", str(tmp_path / "sub2api"))
    monkeypatch.setenv("SUB2API_GROK_GROUP_ID", "5")
    monkeypatch.setenv("SUB2API_POSTGRES_CONTAINER", "postgres")
    monkeypatch.setenv("SUB2API_PG_USER", "sub2api")
    monkeypatch.setenv("SUB2API_PG_DB", "sub2api")
    monkeypatch.setenv("BRIDGE_MANAGEMENT_KEY_FILE", str(tmp_path / "management"))
    monkeypatch.setenv("BRIDGE_JWT_SECRET_FILE", str(tmp_path / "jwt"))
    spec = importlib.util.spec_from_file_location("bridge_source_test", BRIDGE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bridge_uses_secret_files_and_signed_mailbox_tokens(monkeypatch, tmp_path):
    bridge = load_bridge(monkeypatch, tmp_path)
    token = bridge.sign_jwt({"email": "test@example.com", "password": "hidden", "exp": int(time.time()) + 60})
    payload = bridge.verify_jwt(token)
    assert payload["email"] == "test@example.com"
    assert bridge.SUB2API_GROK_GROUP_ID == 5
    assert bridge.MAILU_DOMAINS == ("example.com", "alt.example.com")


def test_bridge_probe_parses_json(monkeypatch, tmp_path):
    bridge = load_bridge(monkeypatch, tmp_path)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            return json.dumps({"data": {"success": True}}).encode()

    monkeypatch.setattr(bridge.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    assert bridge.test_sub2api_account(1) is True


def test_bridge_probe_parses_sse(monkeypatch, tmp_path):
    bridge = load_bridge(monkeypatch, tmp_path)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            return (
                b'data: {"type":"test_start","model":"grok-4.6"}\n\n'
                b'data: {"type":"content","text":"OK"}\n\n'
                b'data: {"type":"test_complete","success":true}\n\n'
            )

    monkeypatch.setattr(bridge.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    assert bridge.test_sub2api_account(1) is True


def test_bridge_probe_rejects_incomplete_sse(monkeypatch, tmp_path):
    bridge = load_bridge(monkeypatch, tmp_path)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            return b'data: {"type":"test_start","success":true}\n\n'

    monkeypatch.setattr(bridge.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    assert bridge.test_sub2api_account(1) is False


def test_account_probe_treats_quota_as_usable(monkeypatch, tmp_path):
    bridge = load_bridge(monkeypatch, tmp_path)
    body = b'{"error":"spending-limit: run out of credit"}'
    assert bridge.account_test_result(body) == "usable_exhausted"


def test_bridge_source_contains_no_embedded_production_secret():
    source = BRIDGE.read_text(encoding="utf-8")
    assert "mailu_api_" not in source
    assert "sub2api_admin_" not in source


def test_classify_probe_payload_categories(monkeypatch, tmp_path):
    bridge = load_bridge(monkeypatch, tmp_path)
    assert bridge.classify_probe_payload(200, '{"id":"x"}')[0] == "upstream_error"
    cat, code, msg = bridge.classify_probe_payload(429, '{"code":"subscription:free-usage-exhausted"}')
    assert cat == "rate_limited" and code == "RATE_LIMITED"
    cat, code, msg = bridge.classify_probe_payload(403, '{"code":"permission-denied","error":"Access to the chat endpoint is denied."}')
    assert cat == "permission_denied" and code == "PERMISSION_DENIED"
    cat, code, msg = bridge.classify_probe_payload(401, '{"error":"invalid credentials"}')
    assert cat == "token_bad" and code == "TOKEN_INVALID"


def test_direct_probe_requires_assistant_marker_and_full_headers(monkeypatch, tmp_path):
    bridge = load_bridge(monkeypatch, tmp_path)
    monkeypatch.setattr(bridge.secrets, "token_urlsafe", lambda size: "fixed-nonce")
    captured = {}

    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            return json.dumps(self.payload).encode()

    response = Response({
        "status": "completed",
        "input": "Reply exactly: bridge-fixed-nonce",
        "output": [],
    })

    def urlopen(request, timeout):
        captured["request"] = request
        return response

    monkeypatch.setattr(bridge.urllib.request, "urlopen", urlopen)
    auth = {"access_token": "token"}
    assert bridge._probe_auth_once(auth, timeout=1)["ok"] is False

    headers = {key.lower(): value for key, value in captured["request"].header_items()}
    assert headers["x-xai-token-auth"] == "xai-grok-cli"
    assert headers["x-grok-client-identifier"] == "grok-shell"
    assert headers["user-agent"] == "grok-cli/0.2.93"
    request_body = json.loads(captured["request"].data)
    assert request_body["max_output_tokens"] >= 64

    response.payload = {
        "status": "completed",
        "output": [{
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "bridge-fixed-nonce"}],
        }],
    }
    assert bridge._probe_auth_once(auth, timeout=1)["ok"] is True


def test_restore_account_verifies_token_groups_and_schedulable(monkeypatch, tmp_path):
    bridge = load_bridge(monkeypatch, tmp_path)
    snapshot = {
        "id": 10,
        "name": "grok_test_example.com",
        "platform": "grok",
        "type": "oauth",
        "credentials": {"access_token": "old-token", "refresh_token": "refresh"},
        "extra": {},
        "group_ids": [5],
        "schedulable": True,
        "concurrency": 1,
        "priority": 1,
    }
    calls = []
    monkeypatch.setattr(bridge, "sub2api_api", lambda method, path, body=None: calls.append((method, path, body)) or {})
    monkeypatch.setattr(
        bridge,
        "account_state_fingerprint",
        lambda account_id: {
            "id": account_id,
            "token_hash": bridge.hashlib.sha256(b"old-token").hexdigest(),
            "refresh_hash": bridge.hashlib.sha256(b"refresh").hexdigest(),
            "group_ids": [5],
            "schedulable": True,
        },
    )
    assert bridge.restore_sub2api_account(snapshot) is True
    assert calls[0][0] == "PUT"
    assert calls[1][2] == {"schedulable": True}


def test_build_account_payload_normalizes_expires_at(monkeypatch, tmp_path):
    bridge = load_bridge(monkeypatch, tmp_path)
    token = jwt_token(100, 21700)
    payload = bridge.build_account_payload(
        "grok_test_example.com",
        {"access_token": token, "refresh_token": "r" * 32, "email": "test@example.com"},
        [],
        False,
    )
    assert payload["credentials"]["expires_at"] == "21700"


def test_stale_auth_rejects_older_rotated_refresh(monkeypatch, tmp_path):
    bridge = load_bridge(monkeypatch, tmp_path)
    current = {
        "credentials": {"access_token": jwt_token(200, 21800), "refresh_token": "new-refresh"},
    }
    old = {"access_token": jwt_token(100, 21700), "refresh_token": "old-refresh"}
    remint = {"access_token": jwt_token(300, 21900), "refresh_token": "remint-refresh"}
    assert bridge.auth_is_stale(current, old) is True
    assert bridge.auth_is_stale(current, remint) is False


def test_subject_mismatch_is_rejected(monkeypatch, tmp_path):
    bridge = load_bridge(monkeypatch, tmp_path)
    current = {
        "credentials": {"access_token": jwt_token(200, 21800, "current-sub"), "refresh_token": "current"},
    }
    candidate = {"access_token": jwt_token(300, 21900, "other-sub"), "refresh_token": "candidate"}
    assert bridge.auth_subject_mismatch(current, candidate) is True


def test_stale_auth_endpoint_performs_no_sub2api_write(monkeypatch, tmp_path):
    bridge = load_bridge(monkeypatch, tmp_path)
    now = int(time.time())
    candidate = {
        "email": "test@example.com",
        "access_token": jwt_token(now - 120, now + 21000),
        "refresh_token": "o" * 32,
    }
    snapshot = {
        "id": 10,
        "credentials": {
            "access_token": jwt_token(now - 60, now + 21200),
            "refresh_token": "n" * 32,
        },
    }
    writes = []
    monkeypatch.setattr(bridge, "probe_auth_direct", lambda auth: {"ok": True, "category": "ok"})
    monkeypatch.setattr(bridge, "find_account_snapshot", lambda name, email, subject: snapshot)
    monkeypatch.setattr(bridge, "sub2api_api", lambda *args, **kwargs: writes.append((args, kwargs)))
    raw = json.dumps(candidate).encode()
    environ = {
        "PATH_INFO": "/v0/management/auth-files",
        "REQUEST_METHOD": "POST",
        "CONTENT_LENGTH": str(len(raw)),
        "QUERY_STRING": "name=xai-test.json",
        "HTTP_X_MANAGEMENT_KEY": bridge.MANAGEMENT_KEY,
        "wsgi.input": io.BytesIO(raw),
    }
    status = {}
    body = b"".join(bridge.handle_request(environ, lambda value, headers: status.update(value=value)))
    result = json.loads(body)
    assert status["value"].startswith("422")
    assert result["error_code"] == "STALE_AUTH"
    assert result["imported"] is False
    assert writes == []


def test_identity_lookup_uses_email_and_subject_and_rejects_ambiguity(monkeypatch, tmp_path):
    bridge = load_bridge(monkeypatch, tmp_path)
    queries = []
    snapshot = {"id": 10, "name": "zzzz-grok-test"}
    monkeypatch.setattr(bridge, "_psql_json", lambda query: queries.append(query) or [snapshot])

    assert bridge.find_account_snapshot(
        "grok_test_example.com", "test@example.com", "subject'value",
    ) == snapshot
    assert "credentials->>'email'" in queries[0]
    assert "credentials->>'sub'" in queries[0]
    assert "subject''value" in queries[0]

    monkeypatch.setattr(bridge, "_psql_json", lambda query: [snapshot, {"id": 11}])
    try:
        bridge.find_account_snapshot("grok_test_example.com", "test@example.com", "subject")
    except RuntimeError as exc:
        assert "ambiguous" in str(exc)
    else:
        raise AssertionError("ambiguous identity should fail closed")


def test_update_preserves_display_name_and_priority(monkeypatch, tmp_path):
    bridge = load_bridge(monkeypatch, tmp_path)
    now = int(time.time())
    candidate = {
        "email": "test@example.com",
        "access_token": jwt_token(now - 30, now + 21000, "same-subject"),
        "refresh_token": "n" * 32,
    }
    snapshot = {
        "id": 10,
        "name": "zzzz-grok_test_example.com",
        "priority": 5,
        "credentials": {
            "email": "test@example.com",
            "access_token": jwt_token(now - 120, now + 20500, "same-subject"),
            "refresh_token": "o" * 32,
        },
    }
    writes = []
    lookups = []
    monkeypatch.setattr(bridge, "probe_auth_direct", lambda auth: {"ok": True, "category": "ok"})
    monkeypatch.setattr(
        bridge,
        "find_account_snapshot",
        lambda name, email="", subject="": lookups.append((name, email, subject)) or snapshot,
    )
    monkeypatch.setattr(
        bridge,
        "sub2api_api",
        lambda method, path, body=None: writes.append((method, path, body)) or {},
    )
    monkeypatch.setattr(bridge, "test_sub2api_account_result", lambda account_id: "usable")
    monkeypatch.setattr(bridge, "promote_sub2api_account", lambda account_id: True)
    raw = json.dumps(candidate).encode()
    environ = {
        "PATH_INFO": "/v0/management/auth-files",
        "REQUEST_METHOD": "POST",
        "CONTENT_LENGTH": str(len(raw)),
        "QUERY_STRING": "name=xai-test.json",
        "HTTP_X_MANAGEMENT_KEY": bridge.MANAGEMENT_KEY,
        "wsgi.input": io.BytesIO(raw),
    }
    status = {}
    body = b"".join(bridge.handle_request(environ, lambda value, headers: status.update(value=value)))
    result = json.loads(body)

    assert status["value"].startswith("200")
    assert result["action"] == "updated" and result["account_id"] == 10
    assert lookups[0] == ("grok_test_example.com", "test@example.com", "same-subject")
    update = next(body for method, path, body in writes if method == "PUT" and path.endswith("/10"))
    assert update["name"] == "zzzz-grok_test_example.com"
    assert update["priority"] == 5


def test_promotion_preserves_rotated_credentials(monkeypatch, tmp_path):
    bridge = load_bridge(monkeypatch, tmp_path)
    rotated = {
        "id": 10,
        "token_hash": "access-new",
        "refresh_hash": "refresh-new",
        "token_version": "7",
        "group_ids": [],
        "schedulable": False,
    }
    promoted = dict(rotated, group_ids=[5], schedulable=True)
    states = iter((rotated, promoted))
    calls = []
    monkeypatch.setattr(bridge, "account_state_fingerprint", lambda account_id: next(states))
    monkeypatch.setattr(bridge, "sub2api_api", lambda method, path, body=None: calls.append((method, path, body)) or {})
    assert bridge.promote_sub2api_account(10) is True
    assert calls[0][2] == {"group_ids": [5], "confirm_mixed_channel_risk": True}
    assert "credentials" not in calls[0][2]


def test_created_candidate_with_rotated_token_is_quarantined_not_deleted(monkeypatch, tmp_path):
    bridge = load_bridge(monkeypatch, tmp_path)
    candidate = {"access_token": jwt_token(100, 21700), "refresh_token": "old"}
    current = {"credentials": {"access_token": jwt_token(200, 21800), "refresh_token": "new"}}
    calls = {"quarantine": 0, "delete": 0}
    monkeypatch.setattr(bridge, "find_account_snapshot_by_id", lambda account_id: current)
    monkeypatch.setattr(
        bridge,
        "quarantine_sub2api_account",
        lambda *args: calls.__setitem__("quarantine", calls["quarantine"] + 1) or True,
    )
    monkeypatch.setattr(
        bridge,
        "delete_sub2api_account",
        lambda *args: calls.__setitem__("delete", calls["delete"] + 1) or True,
    )
    ok, state = bridge.rollback_import_candidate(10, "created", None, "name", candidate, True)
    assert ok is True and state == "quarantined"
    assert calls == {"quarantine": 1, "delete": 0}


def test_auth_push_middleware_serializes_same_identity(monkeypatch, tmp_path):
    bridge = load_bridge(monkeypatch, tmp_path)
    active = 0
    maximum = 0
    guard = threading.Lock()

    def app(environ, start_response):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.03)
        with guard:
            active -= 1
        return [b"ok"]

    wrapped = bridge.serialize_auth_push(app)
    raw = json.dumps({"email": "same@example.com"}).encode()

    def invoke():
        environ = {
            "PATH_INFO": "/v0/management/auth-files",
            "REQUEST_METHOD": "POST",
            "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": io.BytesIO(raw),
        }
        assert b"".join(wrapped(environ, lambda *args: None)) == b"ok"

    threads = [threading.Thread(target=invoke) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert maximum == 1
