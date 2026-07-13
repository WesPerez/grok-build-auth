#!/usr/bin/env python3
"""Grok Register 桥接服务
- Cloudflare Temp Email 兼容 API（用 Mailu 做后端）
- Sub2API auth 推送端点（兼容历史 CPA 管理路径）
"""

import email
import base64
import hashlib
import hmac
import imaplib
import io
import json
import os
import re
import secrets
import ssl
import stat
import string
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

# ─── 配置 ───────────────────────────────────────────────

def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def secret_from_env_or_file(name, file_name):
    value = os.environ.get(name, "").strip()
    if value:
        return value
    path = os.environ.get(file_name, "").strip()
    if not path:
        raise RuntimeError(f"set {name} or {file_name}")
    secret_path = Path(path).expanduser().resolve()
    if os.name != "nt" and stat.S_IMODE(secret_path.stat().st_mode) & 0o077:
        raise RuntimeError(f"secret file permissions must be 0600: {secret_path}")
    value = secret_path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"secret file is empty: {secret_path}")
    return value


MAILU_API_TOKEN = secret_from_env_or_file("MAILU_API_TOKEN", "MAILU_API_TOKEN_FILE")
MAILU_API_BASE = required_env("MAILU_API_BASE").rstrip("/")
MAILU_DOMAIN = required_env("MAILU_DOMAIN").lower()
MAILU_DOMAINS = tuple(dict.fromkeys(
    domain.strip().lower()
    for domain in os.environ.get("MAILU_DOMAINS", MAILU_DOMAIN).split(",")
    if domain.strip()
))
if MAILU_DOMAIN not in MAILU_DOMAINS:
    MAILU_DOMAINS = (MAILU_DOMAIN, *MAILU_DOMAINS)
MAILU_IMAP_HOST = required_env("MAILU_IMAP_HOST")
MAILU_IMAP_PORT = int(os.environ.get("MAILU_IMAP_PORT", "993"))

SUB2API_BASE = os.environ.get("SUB2API_BASE", "http://127.0.0.1:13080").rstrip("/")
SUB2API_ADMIN_KEY = secret_from_env_or_file("SUB2API_ADMIN_KEY", "SUB2API_ADMIN_KEY_FILE")
SUB2API_GROK_GROUP_ID = int(required_env("SUB2API_GROK_GROUP_ID"))

BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "8190"))
MAX_REQUEST_BODY = 1024 * 1024
GROK_CLI_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
SUB2API_POSTGRES_CONTAINER = required_env("SUB2API_POSTGRES_CONTAINER")
SUB2API_PG_USER = required_env("SUB2API_PG_USER")
SUB2API_PG_DB = required_env("SUB2API_PG_DB")
MANAGEMENT_KEY = secret_from_env_or_file("BRIDGE_MANAGEMENT_KEY", "BRIDGE_MANAGEMENT_KEY_FILE")
BRIDGE_SECRET = secret_from_env_or_file("BRIDGE_JWT_SECRET", "BRIDGE_JWT_SECRET_FILE")
AUTH_PUSH_LOCKS = {}
AUTH_PUSH_LOCKS_GUARD = threading.Lock()

# ─── 工具函数 ───────────────────────────────────────────

def generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))

def generate_password(length=20):
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(chars) for _ in range(length))

def sign_jwt(payload):
    header = json.dumps({"alg": "HS256", "typ": "JWT"}).encode()
    payload = json.dumps(payload).encode()
    b64 = lambda b: __import__("base64").urlsafe_b64encode(b).rstrip(b"=").decode()
    msg = f"{b64(header)}.{b64(payload)}"
    sig = hmac.new(BRIDGE_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    return f"{msg}.{b64(sig)}"

def verify_jwt(token):
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        msg = f"{parts[0]}.{parts[1]}"
        sig = hmac.new(BRIDGE_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
        b64 = lambda b: __import__("base64").urlsafe_b64encode(b).rstrip(b"=").decode()
        if not hmac.compare_digest(parts[2], b64(sig)):
            return None
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(__import__("base64").urlsafe_b64decode(padded))
        if int(payload.get("exp") or 0) <= int(time.time()):
            return None
        return payload
    except Exception:
        return None

def mailu_api(method, path, body=None):
    url = f"{MAILU_API_BASE}{path}"
    headers = {"Authorization": f"Bearer {MAILU_API_TOKEN}", "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    resp = urllib.request.urlopen(req, timeout=15, context=ctx)
    return json.loads(resp.read())

def sub2api_api(method, path, body=None):
    url = f"{SUB2API_BASE}{path}"
    headers = {"x-api-key": SUB2API_ADMIN_KEY, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    resp = urllib.request.urlopen(req, timeout=30)
    return json.loads(resp.read())


def sub2api_api_status(method, path, body=None):
    """Return (status, JSON-or-None) without turning 404 into an exception."""
    url = f"{SUB2API_BASE}{path}"
    headers = {"x-api-key": SUB2API_ADMIN_KEY, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read(1024 * 1024)
            return int(resp.status), json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read(1024 * 1024)
        except Exception:
            pass
        try:
            payload = json.loads(raw) if raw else None
        except Exception:
            payload = None
        return int(exc.code or 0), payload


def email_api_authorized(headers):
    expected = f"Bearer {MANAGEMENT_KEY}"
    return hmac.compare_digest(headers.get("authorization", ""), expected)


def decode_jwt_payload(token):
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("access_token must be a JWT")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def token_times(credentials):
    try:
        claims = decode_jwt_payload(str((credentials or {}).get("access_token") or ""))
        return int(claims.get("iat") or 0), int(claims.get("exp") or 0)
    except Exception:
        return 0, 0


def auth_subject(credentials):
    try:
        claims = decode_jwt_payload(str((credentials or {}).get("access_token") or ""))
        return str(claims.get("sub") or claims.get("principal_id") or "").strip()
    except Exception:
        return str((credentials or {}).get("sub") or "").strip()


def auth_subject_mismatch(snapshot, auth_data):
    if not snapshot:
        return False
    current_sub = auth_subject(snapshot.get("credentials") or {})
    candidate_sub = auth_subject(auth_data)
    return bool(current_sub and candidate_sub and current_sub != candidate_sub)


def auth_is_stale(snapshot, auth_data):
    if not snapshot:
        return False
    current = snapshot.get("credentials") or {}
    current_refresh = str(current.get("refresh_token") or "")
    candidate_refresh = str(auth_data.get("refresh_token") or "")
    if not current_refresh or not candidate_refresh or current_refresh == candidate_refresh:
        return False
    current_access = str(current.get("access_token") or "")
    candidate_access = str(auth_data.get("access_token") or "")
    if current_access and current_access == candidate_access:
        return True
    current_iat, _ = token_times(current)
    candidate_iat, _ = token_times(auth_data)
    return bool(current_iat and candidate_iat and candidate_iat <= current_iat)


def auth_push_lock(identity):
    with AUTH_PUSH_LOCKS_GUARD:
        lock = AUTH_PUSH_LOCKS.get(identity)
        if lock is None:
            lock = threading.Lock()
            AUTH_PUSH_LOCKS[identity] = lock
        return lock


def serialize_auth_push(app):
    def wrapped(environ, start_response):
        if environ.get("PATH_INFO") != "/v0/management/auth-files" or environ.get("REQUEST_METHOD") != "POST":
            return app(environ, start_response)
        raw_body = b""
        try:
            body_len = int(environ.get("CONTENT_LENGTH", 0))
            raw_body = environ["wsgi.input"].read(body_len) if body_len > 0 else b""
            payload = json.loads(raw_body) if raw_body else {}
            identity = str(payload.get("email") or payload.get("sub") or "invalid").strip().lower()
        except Exception:
            identity = "invalid"
        environ["wsgi.input"] = io.BytesIO(raw_body)
        with auth_push_lock(identity):
            return app(environ, start_response)
    return wrapped


def credentials_advanced_after_candidate(current, candidate):
    if not current or not candidate:
        return False
    if str(current.get("refresh_token") or "") == str(candidate.get("refresh_token") or ""):
        return False
    current_iat, _ = token_times(current)
    candidate_iat, _ = token_times(candidate)
    if current_iat and candidate_iat:
        return current_iat > candidate_iat
    return bool(current.get("_token_version"))


def validate_auth_data(auth_data):
    email_addr = str(auth_data.get("email") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._+-]{0,63}@[a-z0-9.-]+", email_addr):
        raise ValueError("invalid email field")
    access_token = str(auth_data.get("access_token") or "").strip()
    refresh_token = str(auth_data.get("refresh_token") or "").strip()
    if len(access_token) < 100 or len(refresh_token) < 20:
        raise ValueError("incomplete OAuth credentials")
    claims = decode_jwt_payload(access_token)
    if int(claims.get("exp") or 0) <= int(time.time()) + 60:
        raise ValueError("access_token is expired or near expiry")
    if not str(claims.get("sub") or "").strip():
        raise ValueError("access_token is missing subject")
    return email_addr, access_token


def _psql_json(query):
    proc = subprocess.run(
        ["docker", "exec", SUB2API_POSTGRES_CONTAINER, "psql", "-U", SUB2API_PG_USER,
         "-d", SUB2API_PG_DB, "-Atc", query],
        capture_output=True, text=True, timeout=20, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("account database query failed")
    value = proc.stdout.strip()
    return json.loads(value) if value else None


def find_account_snapshot(name):
    if not re.fullmatch(r"grok_[a-z0-9._+-]+_[a-z0-9.-]+", name):
        raise ValueError("invalid account name")
    query = (
        "select json_build_object("
        "'id',a.id,'name',a.name,'platform',a.platform,'type',a.type,"
        "'credentials',a.credentials,'extra',a.extra,'proxy_id',a.proxy_id,"
        "'concurrency',a.concurrency,'priority',a.priority,'status',a.status,"
        "'schedulable',a.schedulable,'rate_multiplier',a.rate_multiplier,"
        "'group_ids',coalesce((select json_agg(ag.group_id order by ag.group_id) "
        "from account_groups ag where ag.account_id=a.id),'[]'::json)) "
        "from accounts a "
        f"where a.name='{name}' and a.deleted_at is null order by a.id desc limit 1"
    )
    return _psql_json(query)


def find_account_snapshot_by_id(account_id):
    query = (
        "select json_build_object("
        "'id',a.id,'name',a.name,'platform',a.platform,'type',a.type,"
        "'credentials',a.credentials,'extra',a.extra,'proxy_id',a.proxy_id,"
        "'concurrency',a.concurrency,'priority',a.priority,'status',a.status,"
        "'schedulable',a.schedulable,'rate_multiplier',a.rate_multiplier,"
        "'group_ids',coalesce((select json_agg(ag.group_id order by ag.group_id) "
        "from account_groups ag where ag.account_id=a.id),'[]'::json)) "
        "from accounts a "
        f"where a.id={int(account_id)} and a.deleted_at is null"
    )
    return _psql_json(query)


def snapshot_restore_payload(snapshot):
    return {
        "name": snapshot["name"],
        "platform": snapshot["platform"],
        "type": snapshot["type"],
        "credentials": snapshot["credentials"],
        "extra": snapshot.get("extra") or {},
        "proxy_id": snapshot.get("proxy_id"),
        "concurrency": int(snapshot.get("concurrency") or 1),
        "priority": int(snapshot.get("priority") or 1),
        "status": snapshot.get("status") or "active",
        "schedulable": bool(snapshot.get("schedulable")),
        "group_ids": list(snapshot.get("group_ids") or []),
        "rate_multiplier": float(snapshot.get("rate_multiplier") or 1),
        "confirm_mixed_channel_risk": True,
    }


def account_state_fingerprint(account_id):
    query = (
        "select json_build_object("
        "'id',a.id,'access_token',coalesce(a.credentials->>'access_token',''),"
        "'refresh_token',coalesce(a.credentials->>'refresh_token',''),"
        "'token_version',coalesce(a.credentials->>'_token_version',''),"
        "'schedulable',a.schedulable,"
        "'group_ids',coalesce((select json_agg(ag.group_id order by ag.group_id) "
        "from account_groups ag where ag.account_id=a.id),'[]'::json)) "
        f"from accounts a where a.id={int(account_id)} and a.deleted_at is null"
    )
    state = _psql_json(query)
    if not state:
        return None
    token = str(state.pop("access_token", ""))
    state["token_hash"] = hashlib.sha256(token.encode()).hexdigest()
    refresh = str(state.pop("refresh_token", ""))
    state["refresh_hash"] = hashlib.sha256(refresh.encode()).hexdigest()
    return state


def build_account_payload(name, auth_data, group_ids, schedulable):
    fixed_credentials = dict(auth_data)
    fixed_credentials["base_url"] = GROK_CLI_BASE_URL
    _, expires_at = token_times(fixed_credentials)
    if expires_at:
        fixed_credentials["expires_at"] = str(expires_at)
    token_hash = hashlib.sha256(str(auth_data["access_token"]).encode()).hexdigest()
    return {
        "name": name,
        "platform": "grok",
        "type": "oauth",
        "credentials": fixed_credentials,
        "extra": {
            "base_url": GROK_CLI_BASE_URL,
            "access_token_sha256": token_hash,
            "import_source": "grok-register-bridge",
        },
        "concurrency": 1,
        "group_ids": group_ids,
        "priority": 1,
        "status": "active",
        "schedulable": schedulable,
        "confirm_mixed_channel_risk": True,
    }


def account_test_succeeded(raw_body):
    text = raw_body.decode("utf-8", errors="replace")
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                result = json.loads(data)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(result, dict)
                and result.get("type") == "test_complete"
                and result.get("success") is True
            ):
                return True
        return False

    if not isinstance(result, dict):
        return False
    if result.get("success") is True:
        return True
    data = result.get("data")
    return isinstance(data, dict) and data.get("success") is True


def account_test_result(raw_body):
    if account_test_succeeded(raw_body):
        return "usable"
    low = raw_body.decode("utf-8", errors="replace").lower()
    if any(value in low for value in (
        "free-usage", "rolling 24", "resource_exhausted", "spending-limit",
        "run out of credit", "quota exhausted",
    )):
        return "usable_exhausted"
    return "failed"


def extract_assistant_text(payload):
    if not isinstance(payload, dict):
        return ""
    direct = payload.get("output_text")
    if isinstance(direct, str):
        return direct
    chunks = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict) or content.get("type") != "output_text":
                continue
            text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks)


def classify_probe_payload(status_code, body_text):
    """Map a failed upstream probe outcome to a stable client error code."""
    text = (body_text or "")
    low = text.lower()
    if (
        int(status_code) in (429, 402)
        or "free-usage" in low
        or "resource_exhausted" in low
        or "quota" in low
        or "spending-limit" in low
        or "run out of credit" in low
    ):
        return "rate_limited", "RATE_LIMITED", "免费额度已用尽或触发限流，未入库"
    if "permission-denied" in low or "access to the chat endpoint is denied" in low:
        return "permission_denied", "PERMISSION_DENIED", "无聊天权限/资格（TOS/风控/未开通），未入库"
    if int(status_code) == 401 or "invalid_grant" in low or "revok" in low:
        return "token_bad", "TOKEN_INVALID", "token 无效/过期/已吊销，未入库"
    if int(status_code) == 403:
        return "forbidden", "UPSTREAM_FORBIDDEN", "上游拒绝访问，未入库"
    if int(status_code) == 0:
        return "network_error", "PROBE_NETWORK_ERROR", "探测网络失败，未入库"
    return "upstream_error", "UPSTREAM_ERROR", f"上游探测失败 HTTP {status_code}，未入库"


def _probe_auth_once(auth_data, timeout=45):
    """Probe Grok CLI with the raw access_token before any Sub2API write."""
    access_token = str(auth_data.get("access_token") or "").strip()
    marker = "bridge-" + secrets.token_urlsafe(18)
    body = json.dumps({
        "model": "grok-4.5",
        "input": f"Reply exactly: {marker}",
        "stream": False,
        "max_output_tokens": 64,
        "store": False,
    }).encode()
    req = urllib.request.Request(
        f"{GROK_CLI_BASE_URL}/responses",
        data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-XAI-Token-Auth": "xai-grok-cli",
            "x-grok-client-version": "0.2.93",
            "x-grok-client-identifier": "grok-shell",
            "User-Agent": "grok-cli/0.2.93",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(1024 * 1024).decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
            passed = (
                int(resp.status) == 200
                and isinstance(payload, dict)
                and payload.get("status") == "completed"
                and extract_assistant_text(payload).strip() == marker
            )
            return {
                "ok": passed,
                "category": "ok" if passed else "invalid_response",
                "error_code": "" if passed else "INVALID_PROBE_RESPONSE",
                "message": "账号探测通过" if passed else "探测响应未完成或输出不匹配，未入库",
                "http_status": int(resp.status),
            }
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(1024 * 1024).decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        category, code, message = classify_probe_payload(int(e.code or 0), raw)
        return {
            "ok": False,
            "category": category,
            "error_code": code,
            "message": message,
            "http_status": int(e.code or 0),
        }
    except Exception as e:
        return {
            "ok": False,
            "category": "network_error",
            "error_code": "PROBE_NETWORK_ERROR",
            "message": "探测网络失败，未入库",
            "http_status": 0,
            "error_type": type(e).__name__,
        }


def probe_auth_direct(auth_data, timeout=45, attempts=3, retry_delay=2):
    result = {}
    for attempt in range(1, max(1, int(attempts)) + 1):
        result = _probe_auth_once(auth_data, timeout=timeout)
        result["attempts"] = attempt
        if result.get("ok"):
            return result
        retryable = result.get("category") in {
            "network_error", "upstream_error", "forbidden", "invalid_response",
        }
        status = int(result.get("http_status") or 0)
        if status in (402, 429) or not retryable or attempt >= attempts:
            return result
        time.sleep(max(0, float(retry_delay)))
    return result


def test_sub2api_account_result(account_id):
    url = f"{SUB2API_BASE}/api/v1/admin/accounts/{account_id}/test"
    body = json.dumps({
        "model_id": "grok-4.5",
        "prompt": "Reply exactly: bridge-account-ok",
        "mode": "responses",
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"x-api-key": SUB2API_ADMIN_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw_body = resp.read(1024 * 1024)
    return account_test_result(raw_body)


def test_sub2api_account(account_id):
    return test_sub2api_account_result(account_id) in {"usable", "usable_exhausted"}


def delete_sub2api_account(account_id):
    status, _ = sub2api_api_status("DELETE", f"/api/v1/admin/accounts/{account_id}")
    if status not in (200, 204, 404):
        return False
    status, _ = sub2api_api_status("GET", f"/api/v1/admin/accounts/{account_id}")
    return status == 404


def quarantine_sub2api_account(account_id, name, auth_data):
    try:
        sub2api_api(
            "PUT", f"/api/v1/admin/accounts/{account_id}",
            {"group_ids": [], "confirm_mixed_channel_risk": True},
        )
        sub2api_api(
            "POST", f"/api/v1/admin/accounts/{account_id}/schedulable",
            {"schedulable": False},
        )
        state = account_state_fingerprint(account_id)
        return bool(state and not state.get("schedulable") and not state.get("group_ids"))
    except Exception:
        return False


def rollback_import_candidate(account_id, action, snapshot, name, auth_data, preserve_rotated):
    if action == "created":
        current = find_account_snapshot_by_id(account_id)
        current_credentials = (current or {}).get("credentials") or {}
        if preserve_rotated and credentials_advanced_after_candidate(current_credentials, auth_data):
            ok = quarantine_sub2api_account(account_id, name, auth_data)
            return ok, "quarantined" if ok else "unknown"
        ok = delete_sub2api_account(account_id)
        return ok, False if ok else "unknown"
    ok = restore_sub2api_account(snapshot, auth_data if preserve_rotated else None)
    return ok, False if ok else "unknown"


def restore_sub2api_account(snapshot, candidate_auth=None):
    account_id = int(snapshot["id"])
    restore_snapshot = dict(snapshot)
    if candidate_auth:
        current = find_account_snapshot_by_id(account_id)
        current_credentials = (current or {}).get("credentials") or {}
        if credentials_advanced_after_candidate(current_credentials, candidate_auth):
            restore_snapshot["credentials"] = current_credentials
    expected_credentials = restore_snapshot.get("credentials") or {}
    expected_hash = hashlib.sha256(str(expected_credentials.get("access_token") or "").encode()).hexdigest()
    expected_refresh_hash = hashlib.sha256(str(expected_credentials.get("refresh_token") or "").encode()).hexdigest()
    try:
        sub2api_api("PUT", f"/api/v1/admin/accounts/{account_id}", snapshot_restore_payload(restore_snapshot))
        sub2api_api(
            "POST", f"/api/v1/admin/accounts/{account_id}/schedulable",
            {"schedulable": bool(snapshot.get("schedulable"))},
        )
        state = account_state_fingerprint(account_id)
        return bool(
            state
            and state.get("token_hash") == expected_hash
            and state.get("refresh_hash") == expected_refresh_hash
            and list(state.get("group_ids") or []) == list(snapshot.get("group_ids") or [])
            and bool(state.get("schedulable")) == bool(snapshot.get("schedulable"))
        )
    except Exception:
        return False


def promote_sub2api_account(account_id):
    verified_state = account_state_fingerprint(account_id)
    if not verified_state:
        raise RuntimeError("post-test account state missing")
    sub2api_api(
        "PUT", f"/api/v1/admin/accounts/{account_id}",
        {"group_ids": [SUB2API_GROK_GROUP_ID], "confirm_mixed_channel_risk": True},
    )
    sub2api_api(
        "POST", f"/api/v1/admin/accounts/{account_id}/schedulable",
        {"schedulable": True},
    )
    final_state = account_state_fingerprint(account_id)
    return bool(
        final_state
        and final_state.get("token_hash") == verified_state.get("token_hash")
        and final_state.get("refresh_hash") == verified_state.get("refresh_hash")
        and final_state.get("token_version") == verified_state.get("token_version")
        and final_state.get("group_ids") == [SUB2API_GROK_GROUP_ID]
        and final_state.get("schedulable") is True
    )

def imap_fetch_messages(email_addr, password):
    """通过 IMAP 获取邮件列表"""
    ctx = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL(MAILU_IMAP_HOST, MAILU_IMAP_PORT, ssl_context=ctx, timeout=15)
    try:
        conn.login(email_addr, password)
        conn.select("INBOX", readonly=True)
        status, data = conn.search(None, "ALL")
        if status != "OK":
            return []
        ids = data[0].split()
        messages = []
        for mid in ids[-20:]:  # 最近 20 封
            status, msg_data = conn.fetch(mid, "(FLAGS BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE)])")
            if status != "OK":
                continue
            flags = ""
            header_text = ""
            for part in msg_data:
                if isinstance(part, tuple):
                    header_text = part[1].decode(errors="replace")
                elif isinstance(part, bytes):
                    if b"FLAGS" in part:
                        flags = part.decode(errors="replace")
            msg = email.message_from_string(header_text)
            messages.append({
                "id": mid.decode(),
                "subject": str(email.header.make_header(email.header.decode_header(msg["Subject"] or ""))),
                "from": msg["From"] or "",
                "to": msg["To"] or "",
                "date": msg["Date"] or "",
                "seen": "\\Seen" in flags,
            })
        return messages
    finally:
        try:
            conn.logout()
        except Exception:
            pass

def imap_fetch_message(email_addr, password, msg_id):
    """通过 IMAP 获取单封邮件详情"""
    ctx = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL(MAILU_IMAP_HOST, MAILU_IMAP_PORT, ssl_context=ctx, timeout=15)
    try:
        conn.login(email_addr, password)
        conn.select("INBOX", readonly=True)
        status, msg_data = conn.fetch(msg_id.encode(), "(BODY.PEEK[])")
        if status != "OK":
            return None
        for part in msg_data:
            if isinstance(part, tuple):
                raw = part[1]
                msg = email.message_from_bytes(raw)
                subject = str(email.header.make_header(email.header.decode_header(msg["Subject"] or "")))
                text_body = ""
                html_body = ""
                if msg.is_multipart():
                    for sub in msg.walk():
                        ct = sub.get_content_type()
                        if ct == "text/plain":
                            payload = sub.get_payload(decode=True)
                            if payload:
                                text_body += payload.decode(errors="replace")
                        elif ct == "text/html":
                            payload = sub.get_payload(decode=True)
                            if payload:
                                html_body += payload.decode(errors="replace")
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        ct = msg.get_content_type()
                        if ct == "text/html":
                            html_body = payload.decode(errors="replace")
                        else:
                            text_body = payload.decode(errors="replace")
                return {
                    "id": msg_id,
                    "subject": subject,
                    "from": msg["From"] or "",
                    "to": msg["To"] or "",
                    "date": msg["Date"] or "",
                    "text": text_body,
                    "html": [html_body] if html_body else [],
                }
        return None
    finally:
        try:
            conn.logout()
        except Exception:
            pass

# ─── HTTP 路由 ──────────────────────────────────────────

def handle_request(environ, start_response):
    path = environ.get("PATH_INFO", "")
    method = environ.get("REQUEST_METHOD", "GET")
    headers = {}
    # 简易 header 解析
    for key in environ:
        if key.startswith("HTTP_"):
            headers[key[5:].replace("_", "-").lower()] = environ[key]

    def json_response(data, status="200 OK"):
        body = json.dumps(data, ensure_ascii=False).encode()
        start_response(status, [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Access-Control-Allow-Origin", "*"),
        ])
        return [body]

    def error_response(msg, status="400 Bad Request", **extra):
        payload = {"error": msg, "ok": False}
        payload.update({k: v for k, v in extra.items() if v is not None})
        return json_response(payload, status)

    # CORS
    if method == "OPTIONS":
        start_response("204 No Content", [
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Management-Key"),
        ])
        return [b""]

    # ── 临时邮箱 API ──

    if path == "/admin/new_address" and method == "POST":
        if not email_api_authorized(headers):
            return error_response("未授权", "401 Unauthorized")
        try:
            body_len = int(environ.get("CONTENT_LENGTH", 0))
            body = json.loads(environ["wsgi.input"].read(body_len)) if body_len > 0 else {}
        except Exception:
            body = {}
        name = str(body.get("name") or generate_username()).strip().lower()
        domain = str(body.get("domain") or MAILU_DOMAIN).strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", name):
            return error_response("邮箱本地部分无效")
        if domain not in MAILU_DOMAINS:
            return error_response("邮箱域名无效")
        password = generate_password()
        email_addr = f"{name}@{domain}"
        try:
            mailu_api("POST", "/user", {
                "email": email_addr,
                "raw_password": password,
                "quota_bytes": 52428800,
            })
        except Exception as e:
            return error_response(f"创建邮箱失败: {e}", "500 Internal Server Error")
        jwt = sign_jwt({"email": email_addr, "password": password, "exp": int(time.time()) + 3600})
        return json_response({"address": email_addr, "jwt": jwt})

    if path == "/api/mails" and method == "GET":
        auth = headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return error_response("未授权", "401 Unauthorized")
        token_data = verify_jwt(auth[7:])
        if not token_data:
            return error_response("无效 token", "401 Unauthorized")
        try:
            msgs = imap_fetch_messages(token_data["email"], token_data["password"])
            return json_response(msgs)
        except Exception as e:
            return error_response(f"获取邮件失败: {e}", "500 Internal Server Error")

    if re.match(r"^/api/mail/[^/]+$", path) and method == "GET":
        auth = headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return error_response("未授权", "401 Unauthorized")
        token_data = verify_jwt(auth[7:])
        if not token_data:
            return error_response("无效 token", "401 Unauthorized")
        msg_id = path.rsplit("/", 1)[-1]
        try:
            msg = imap_fetch_message(token_data["email"], token_data["password"], msg_id)
            if msg is None:
                return error_response("邮件不存在", "404 Not Found")
            return json_response(msg)
        except Exception as e:
            return error_response(f"获取邮件详情失败: {e}", "500 Internal Server Error")

    if path == "/api/domains" and method == "GET":
        if not email_api_authorized(headers):
            return error_response("未授权", "401 Unauthorized")
        return json_response(list(MAILU_DOMAINS))

    if path == "/api/token" and method == "POST":
        if not email_api_authorized(headers):
            return error_response("未授权", "401 Unauthorized")
        return json_response({"token": "not_used"})

    # ── Sub2API auth 推送端点（兼容历史 CPA 管理路径） ──

    if path == "/v0/management/auth-files" and method == "POST":
        mgmt_key = headers.get("x-management-key", "")
        if mgmt_key != MANAGEMENT_KEY:
            return error_response("管理密钥无效", "403 Forbidden")

        # 读取请求体
        body_len = int(environ.get("CONTENT_LENGTH", 0))
        if body_len < 1 or body_len > MAX_REQUEST_BODY:
            return error_response("请求体大小无效", "413 Payload Too Large")
        raw_body = environ["wsgi.input"].read(body_len)
        try:
            auth_data = json.loads(raw_body)
        except Exception:
            return error_response("无效 JSON", "400 Bad Request")

        # 从 query string 获取文件名
        from urllib.parse import parse_qs
        qs = parse_qs(environ.get("QUERY_STRING", ""))
        filename = qs.get("name", ["unknown.json"])[0]

        try:
            email_addr, _ = validate_auth_data(auth_data)
        except Exception as exc:
            return error_response(str(exc), "400 Bad Request")

        name = f"grok_{email_addr.replace('@', '_')}"
        try:
            # Pre-probe ONLY. Failures never create/update Sub2API rows.
            probe = probe_auth_direct(auth_data)
            preprobe_usable = bool(probe.get("ok")) or probe.get("category") == "rate_limited"
            if not preprobe_usable:
                print(
                    f"[SUB2API] preprobe rejected: email={email_addr} "
                    f"code={probe.get('error_code')} category={probe.get('category')} "
                    f"http={probe.get('http_status')}"
                )
                return error_response(
                    probe.get("message") or "账号探测失败，未入库",
                    "422 Unprocessable Entity",
                    error_code=probe.get("error_code") or "PROBE_FAILED",
                    probe="failed",
                    probe_category=probe.get("category"),
                    probe_http_status=probe.get("http_status"),
                    email=email_addr,
                    filename=filename,
                    imported=False,
                )

            snapshot = find_account_snapshot(name)
            if auth_subject_mismatch(snapshot, auth_data):
                return error_response(
                    "AUTH principal 与 Sub2API 当前账号不一致，拒绝覆盖",
                    "422 Unprocessable Entity",
                    error_code="SUBJECT_MISMATCH",
                    probe="passed",
                    email=email_addr,
                    filename=filename,
                    account_id=int(snapshot["id"]),
                    imported=False,
                )
            if auth_is_stale(snapshot, auth_data):
                return error_response(
                    "AUTH 快照早于 Sub2API 当前凭据，拒绝覆盖",
                    "422 Unprocessable Entity",
                    error_code="STALE_AUTH",
                    probe="passed",
                    email=email_addr,
                    filename=filename,
                    account_id=int(snapshot["id"]),
                    imported=False,
                )
            account_id = int(snapshot["id"]) if snapshot else None
            candidate = build_account_payload(name, auth_data, [], False)
            action = "created" if account_id is None else "updated"
            try:
                if account_id is None:
                    result = sub2api_api("POST", "/api/v1/admin/accounts", candidate)
                    account_id = int(result["data"]["id"])
                else:
                    sub2api_api("PUT", f"/api/v1/admin/accounts/{account_id}", candidate)
            except Exception as exc:
                rollback_ok = False
                rollback_state = "unknown"
                if action == "updated":
                    rollback_ok, rollback_state = rollback_import_candidate(
                        account_id, action, snapshot, name, auth_data, False,
                    )
                else:
                    ambiguous = find_account_snapshot(name)
                    if ambiguous and not auth_subject_mismatch(ambiguous, auth_data):
                        account_id = int(ambiguous["id"])
                        rollback_ok, rollback_state = rollback_import_candidate(
                            account_id, action, snapshot, name, auth_data, False,
                        )
                return error_response(
                    "候选账号写入异常，已回滚" if rollback_ok else "候选账号写入结果不确定",
                    "500 Internal Server Error",
                    error_code="CANDIDATE_WRITE_FAILED" if rollback_ok else "CANDIDATE_WRITE_UNKNOWN",
                    probe="error",
                    detail=type(exc).__name__,
                    email=email_addr,
                    filename=filename,
                    account_id=account_id,
                    action=action,
                    imported=rollback_state,
                    rollback_succeeded=rollback_ok,
                )

            # Candidate credentials must remain isolated until the Sub2API path passes.
            try:
                sub2api_api(
                    "POST",
                    f"/api/v1/admin/accounts/{account_id}/schedulable",
                    {"schedulable": False},
                )
            except Exception as exc:
                rollback_ok, rollback_state = rollback_import_candidate(
                    account_id, action, snapshot, name, auth_data, False,
                )
                return error_response(
                    "候选账号隔离失败，已回滚" if rollback_ok else "候选账号隔离失败且回滚未完成",
                    "500 Internal Server Error",
                    error_code="CANDIDATE_ISOLATION_FAILED" if rollback_ok else "ROLLBACK_FAILED",
                    probe="error",
                    account_id=account_id,
                    action=action,
                    imported=rollback_state,
                )

            # Post-import verify while isolated, then promote only after success.
            try:
                account_probe = test_sub2api_account_result(account_id)
                if account_probe not in {"usable", "usable_exhausted"}:
                    rollback_ok, rollback_state = rollback_import_candidate(
                        account_id, action, snapshot, name, auth_data, True,
                    )
                    return error_response(
                        "Sub2API 探测失败，已回滚" if rollback_ok else "Sub2API 探测失败且回滚未完成",
                        "422 Unprocessable Entity" if rollback_ok else "500 Internal Server Error",
                        error_code="POSTIMPORT_PROBE_FAILED" if rollback_ok else "ROLLBACK_FAILED",
                        probe="failed",
                        probe_category="postimport_failed",
                        email=email_addr,
                        filename=filename,
                        account_id=account_id,
                        imported=rollback_state,
                        action=action,
                        rollback_succeeded=rollback_ok,
                    )
            except Exception as exc:
                rollback_ok, rollback_state = rollback_import_candidate(
                    account_id, action, snapshot, name, auth_data, True,
                )
                return error_response(
                    "Sub2API 探测异常，已回滚" if rollback_ok else "Sub2API 探测异常且回滚未完成",
                    "422 Unprocessable Entity" if rollback_ok else "500 Internal Server Error",
                    error_code="POSTIMPORT_PROBE_ERROR" if rollback_ok else "ROLLBACK_FAILED",
                    probe="failed",
                    probe_category="postimport_error",
                    detail=f"{type(exc).__name__}",
                    email=email_addr,
                    filename=filename,
                    imported=rollback_state,
                    action=action,
                    rollback_succeeded=rollback_ok,
                )

            try:
                if not promote_sub2api_account(account_id):
                    raise RuntimeError("promoted account state mismatch")
            except Exception as exc:
                rollback_ok, rollback_state = rollback_import_candidate(
                    account_id, action, snapshot, name, auth_data, True,
                )
                if not rollback_ok:
                    quarantine_sub2api_account(account_id, name, auth_data)
                return error_response(
                    "账号提升失败，已回滚" if rollback_ok else "账号提升失败且回滚未完成",
                    "500 Internal Server Error",
                    error_code="PROMOTION_FAILED" if rollback_ok else "ROLLBACK_FAILED",
                    probe="error",
                    detail=type(exc).__name__,
                    email=email_addr,
                    filename=filename,
                    account_id=account_id,
                    imported=rollback_state,
                    action=action,
                    rollback_succeeded=rollback_ok,
                )

            print(f"[SUB2API] imported and probed: account_id={account_id} action={action}")
            return json_response({
                "status": "ok",
                "ok": True,
                "email": email_addr,
                "filename": filename,
                "account_id": account_id,
                "action": action,
                "probe": "passed",
                "availability": account_probe,
                "error_code": "",
                "imported": True,
                "group_id": SUB2API_GROK_GROUP_ID,
            })
        except Exception as e:
            print(f"[SUB2API] import failed: {type(e).__name__}")
            return error_response(
                "导入 Sub2API 失败",
                "500 Internal Server Error",
                error_code="IMPORT_FAILED",
                probe="error",
                detail=f"{type(e).__name__}",
                email=email_addr,
                filename=filename,
                imported=False,
            )

    # ── 健康检查 ──
    if path == "/health":
        return json_response({"status": "ok", "domain": MAILU_DOMAIN})

    return error_response("Not Found", "404 Not Found")


# ─── 启动 ───────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Bridge 端口: {BRIDGE_PORT}")
    print(f"Mailu 域名: {MAILU_DOMAIN}")
    print(f"Sub2API: {SUB2API_BASE}")

    class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    server = make_server(
        "127.0.0.1",
        BRIDGE_PORT,
        serialize_auth_push(handle_request),
        server_class=ThreadingWSGIServer,
    )
    print(f"服务启动在 http://127.0.0.1:{BRIDGE_PORT}")
    server.serve_forever()
