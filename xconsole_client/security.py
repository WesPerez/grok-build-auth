"""Security primitives shared by server-hardened entry points."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


CLIPROXYAPI_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})


def set_restrictive_umask() -> None:
    os.umask(0o077)


def validate_loopback_host(host: str) -> str:
    if host not in LOOPBACK_HOSTS:
        raise ValueError("OAuth callback host must be 127.0.0.1 or ::1")
    return host


def format_loopback_host(host: str) -> str:
    validate_loopback_host(host)
    return f"[{host}]" if ":" in host else host


def validate_cliproxyapi_base_url(value: str) -> str:
    parsed = urlsplit((value or "").strip())
    if (
        parsed.scheme != "https"
        or parsed.hostname != "cli-chat-proxy.grok.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/v1"
    ):
        raise ValueError(f"base_url must be exactly {CLIPROXYAPI_BASE_URL}")
    return CLIPROXYAPI_BASE_URL


def sanitize_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except Exception:
        return "<redacted-url>"


def mask_email(value: str) -> str:
    value = (value or "").strip()
    if "@" not in value:
        return "<redacted-email>" if value else "(unknown)"
    local, domain = value.rsplit("@", 1)
    return f"{local[:1] or '*'}***@{domain}"


_SECRET_FIELD = re.compile(
    r"(?i)(access_token|refresh_token|id_token|authorization|cookie|password|sso|code(?:_verifier)?)"
    r"\s*[:=]\s*([^\s,;&]+)"
)
_JSON_SECRET_FIELD = re.compile(
    r'(?i)(["\'](?:access_token|refresh_token|id_token|authorization|cookie|password|sso|'
    r'code(?:_verifier)?)["\']\s*:\s*["\'])([^"\']+)(["\'])'
)


def redact_text(value: Any) -> str:
    text = str(value)
    text = re.sub(r"https?://[^\s]+", lambda m: sanitize_url(m.group(0)), text)
    text = _JSON_SECRET_FIELD.sub(lambda m: f"{m.group(1)}<redacted>{m.group(3)}", text)
    text = _SECRET_FIELD.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    text = re.sub(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}(?:\.[A-Za-z0-9_-]{10,})?\b", "<redacted-jwt>", text)
    return text[:500]


def secure_json_write(path: str | Path, data: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        os.chmod(target, 0o600)
        dir_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return target
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
