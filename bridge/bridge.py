#!/usr/bin/env python3
"""Grok Register 桥接服务
- Cloudflare Temp Email 兼容 API（用 Mailu 做后端）
- CPA 推送端点（接收后导入 Sub2API）
"""

import email
import base64
import hashlib
import hmac
import imaplib
import json
import os
import re
import secrets
import ssl
import stat
import string
import subprocess
import time
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


def email_api_authorized(headers):
    expected = f"Bearer {MANAGEMENT_KEY}"
    return hmac.compare_digest(headers.get("authorization", ""), expected)


def decode_jwt_payload(token):
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("access_token must be a JWT")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


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


def find_account_id(name):
    if not re.fullmatch(r"grok_[a-z0-9._+-]+_[a-z0-9.-]+", name):
        raise ValueError("invalid account name")
    query = (
        "select id from accounts "
        f"where name='{name}' and deleted_at is null order by id desc limit 1"
    )
    proc = subprocess.run(
        ["docker", "exec", SUB2API_POSTGRES_CONTAINER, "psql", "-U", SUB2API_PG_USER,
         "-d", SUB2API_PG_DB, "-Atc", query],
        capture_output=True, text=True, timeout=15, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("account lookup failed")
    value = proc.stdout.strip()
    return int(value) if value else None


def build_account_payload(name, auth_data, group_ids, schedulable):
    fixed_credentials = dict(auth_data)
    fixed_credentials["base_url"] = GROK_CLI_BASE_URL
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


def test_sub2api_account(account_id):
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
        result = json.loads(resp.read(1024 * 1024))
    if not isinstance(result, dict):
        return False
    if result.get("success") is True:
        return True
    data = result.get("data")
    return isinstance(data, dict) and data.get("success") is True


def test_sub2api_account_with_retry(account_id, max_wait=180, interval=30):
    deadline = time.monotonic() + max_wait
    attempts = 0
    while True:
        attempts += 1
        try:
            if test_sub2api_account(account_id):
                return True, attempts
        except Exception:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, attempts
        time.sleep(min(interval, remaining))

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

    def error_response(msg, status="400 Bad Request"):
        return json_response({"error": msg}, status)

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
        if domain != MAILU_DOMAIN:
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
        return json_response([MAILU_DOMAIN])

    if path == "/api/token" and method == "POST":
        if not email_api_authorized(headers):
            return error_response("未授权", "401 Unauthorized")
        return json_response({"token": "not_used"})

    # ── CPA 推送端点 ──

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
            account_id = find_account_id(name)
            quarantine = build_account_payload(name, auth_data, [], False)
            if account_id is None:
                result = sub2api_api("POST", "/api/v1/admin/accounts", quarantine)
                account_id = int(result["data"]["id"])
                action = "created"
            else:
                sub2api_api("PUT", f"/api/v1/admin/accounts/{account_id}", quarantine)
                action = "updated"
            probe_ok, probe_attempts = test_sub2api_account_with_retry(account_id)
            if not probe_ok:
                print(f"[CPA] probe failed: account_id={account_id} attempts={probe_attempts}")
                return error_response("账号探测失败，已隔离未入组", "422 Unprocessable Entity")
            promoted = build_account_payload(name, auth_data, [SUB2API_GROK_GROUP_ID], True)
            sub2api_api("PUT", f"/api/v1/admin/accounts/{account_id}", promoted)
            print(f"[CPA] imported and probed: account_id={account_id} action={action} attempts={probe_attempts}")
            return json_response({
                "status": "ok", "email": email_addr, "filename": filename,
                "account_id": account_id, "action": action, "probe": "passed",
                "probe_attempts": probe_attempts,
            })
        except Exception as e:
            print(f"[CPA] import failed: {type(e).__name__}")
            return error_response("导入 Sub2API 失败", "500 Internal Server Error")

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

    server = make_server("127.0.0.1", BRIDGE_PORT, handle_request, server_class=ThreadingWSGIServer)
    print(f"服务启动在 http://127.0.0.1:{BRIDGE_PORT}")
    server.serve_forever()
