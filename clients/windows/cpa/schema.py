"""Sub2API xAI auth JSON 组装，使用 router-for-me/CLIProxyAPI-compatible schema。
internal/auth/xai/token.go 的 TokenStorage 结构。

生成的 xai-<email>.json 可被兼容实现直接加载；Sub2API 请求 grok 时会
自带 x-grok-client-version 头（xai_executor.go 硬编码 0.2.93），免费 Build 账号
不会 426。
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from typing import Any

# 对齐 CLIProxyAPI internal/auth/xai/types.go
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
ISSUER = "https://auth.x.ai"
TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"
REDIRECT_URI = "http://127.0.0.1:56121/callback"
# 免费 Grok 4.5 (Build) 走 cli-chat-proxy；付费 API 用 https://api.x.ai/v1
CLI_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
API_BASE_URL = "https://api.x.ai/v1"
DEFAULT_BASE_URL = CLI_BASE_URL


def _sanitize_file_segment(value: str) -> str:
    """对齐兼容 schema 的 CredentialFileName 清洗规则。"""
    value = (value or "").strip()
    if not value:
        return ""
    out: list[str] = []
    for ch in value:
        if (
            ("a" <= ch <= "z")
            or ("A" <= ch <= "Z")
            or ("0" <= ch <= "9")
            or ch in {"@", ".", "_", "-"}
        ):
            out.append(ch)
        else:
            out.append("-")
    return "".join(out).strip("-")


def credential_file_name(email: str = "", sub: str = "") -> str:
    """返回 Sub2API auth 文件名：xai-<email>.json。"""
    email_s = _sanitize_file_segment(email)
    if email_s:
        return f"xai-{email_s}.json"
    sub_s = _sanitize_file_segment(sub)
    if sub_s:
        return f"xai-{sub_s}.json"
    ts = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    return f"xai-{ts}.json"


def _jwt_payload(token: str) -> dict[str, Any]:
    parts = (token or "").split(".")
    if len(parts) < 2:
        raise ValueError("not a JWT")
    seg = parts[1]
    seg += "=" * (-len(seg) % 4)
    return json.loads(base64.urlsafe_b64decode(seg))


def expired_from_access_token(access_token: str) -> tuple[str, int, str]:
    """从 access_token 解析 (expired_rfc3339, expires_in, sub)。"""
    pl = _jwt_payload(access_token)
    exp = int(pl["exp"])
    iat = int(pl["iat"]) if pl.get("iat") is not None else exp - 21600
    expired = datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sub = str(pl.get("sub") or pl.get("principal_id") or "").strip()
    return expired, max(exp - iat, 0), sub


def build_cpa_xai_auth(
    *,
    email: str,
    access_token: str,
    refresh_token: str,
    sub: str | None = None,
    id_token: str | None = None,
    expires_in: int | None = None,
    expired: str | None = None,
    last_refresh: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    token_endpoint: str = TOKEN_ENDPOINT,
    redirect_uri: str = REDIRECT_URI,
) -> dict[str, Any]:
    """组装一个 CLIProxyAPI 可导入的 xai auth 对象（TokenStorage 字段）。"""
    access_token = (access_token or "").strip()
    refresh_token = (refresh_token or "").strip()
    if not access_token:
        raise ValueError("access_token is required")
    if not refresh_token:
        raise ValueError("refresh_token is required (Sub2API 无法在缺 refresh_token 时续期)")

    try:
        exp_s, exp_in, sub_jwt = expired_from_access_token(access_token)
    except Exception:
        exp_s, exp_in, sub_jwt = "", 21600, ""

    if not expired:
        expired = exp_s
    if expires_in is None:
        expires_in = exp_in or 21600
    if not sub:
        sub = sub_jwt
    if not last_refresh:
        last_refresh = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
    if not re.search(r"/v1$", base_url) and base_url.endswith("cli-chat-proxy.grok.com"):
        base_url = base_url + "/v1"

    payload: dict[str, Any] = {
        "type": "xai",
        "auth_kind": "oauth",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": int(expires_in),
        "expired": expired,
        "last_refresh": last_refresh,
        "email": (email or "").strip(),
        "sub": (sub or "").strip(),
        "base_url": base_url,
        "token_endpoint": token_endpoint,
        "redirect_uri": redirect_uri,
    }
    if id_token:
        payload["id_token"] = id_token.strip()
    return payload
