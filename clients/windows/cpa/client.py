"""把 CPA xai auth JSON 推送到远端 CLIProxyAPI 导入。

对齐 router-for-me/CLIProxyAPI 管理 API：
  POST {base}/v0/management/auth-files?name=xai-<email>.json
  X-Management-Key: <secret-key>     (管理员密钥)
  body = 原始 auth JSON
  成功 -> 200 {"status":"ok"}
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

UPLOAD_PATH = "/v0/management/auth-files"


class CpaPushError(RuntimeError):
    pass


def _opener(proxy: str | None, verify_tls: bool) -> urllib.request.OpenerDirector:
    handlers: list[Any] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        # 远端管理口通常在内网/直连，显式禁用系统代理，避免误走 mint 代理
        handlers.append(urllib.request.ProxyHandler({}))
    ctx = None
    if not verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def push_auth_file(
    *,
    remote_base: str,
    secret: str,
    filename: str,
    payload: dict | bytes | str,
    proxy: str | None = None,
    verify_tls: bool = True,
    timeout: float = 30.0,
) -> tuple[bool, int, str]:
    """把一个 auth 文件推送到远端 CLIProxyAPI。返回 (ok, status, text)。"""
    base = (remote_base or "").strip().rstrip("/")
    if not base:
        raise CpaPushError("cpa_remote_base 未配置")
    if not secret:
        raise CpaPushError("cpa_remote_secret 未配置")
    if "://" not in base:
        base = "http://" + base
    if not filename.endswith(".json"):
        filename += ".json"

    if isinstance(payload, dict):
        body = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    elif isinstance(payload, str):
        body = payload.encode("utf-8")
    else:
        body = payload

    url = f"{base}{UPLOAD_PATH}?name={urllib.parse.quote(filename)}"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "X-Management-Key": secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "grok-reg-cpa-push/1.0",
        },
    )
    opener = _opener(proxy, verify_tls)
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            text = resp.read().decode("utf-8", errors="replace")
            return (200 <= status < 300), status, text
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        return False, int(e.code), text
    except Exception as e:  # noqa: BLE001
        raise CpaPushError(str(e)) from e
