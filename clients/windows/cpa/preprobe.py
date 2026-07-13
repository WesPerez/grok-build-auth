"""Client-side Grok Build /responses preprobe.

Mirrors the server agent's guidance and the production CLI header set from
`xai_build_quota_probe.py`. Goal: reject unusable tokens *before* writing
formal `cpa_auths/xai-*.json` or pushing to bridge/Sub2API.

Decision codes (stable, safe to log — never include tokens or full bodies):
- pass / PROBE_PASSED
- reject / MALFORMED_AUTH
- retry / PERMISSION_DENIED | PROBE_NETWORK_ERROR | INVALID_RESPONSE |
  INCOMPLETE_RESPONSE | UPSTREAM_ERROR
- cooldown / RATE_LIMITED
- refresh / TOKEN_INVALID
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

URL = "https://cli-chat-proxy.grok.com/v1/responses"
MARKER = "CLIENT_PROBE_OK"

# Full CLI header set used by production quota probe / real Grok CLI.
# Missing X-XAI-Token-Auth / x-grok-client-identifier can create false negatives.
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "grok-cli/0.2.93",
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-grok-client-version": "0.2.93",
    "x-grok-client-identifier": "grok-shell",
}


def probe_auth(auth: dict[str, Any], *, proxy: str = "", timeout: float = 45) -> dict[str, Any]:
    """Probe one CPA-shaped auth payload against Grok CLI /responses.

    Returns a dict with at least:
      decision: pass|reject|retry|cooldown|refresh
      code: stable machine code
      status: HTTP status or 0
    Never returns token material or full response bodies.
    """
    token = str(auth.get("access_token") or "").strip()
    refresh = str(auth.get("refresh_token") or "").strip()
    if not token or not refresh:
        return {"decision": "reject", "code": "MALFORMED_AUTH", "status": 0}

    session = requests.Session()
    session.trust_env = False
    proxies = {"http": proxy, "https": proxy} if proxy else None

    try:
        response = session.post(
            URL,
            headers={**HEADERS, "Authorization": f"Bearer {token}"},
            json={
                "model": "grok-4.5",
                "input": f"Reply exactly: {MARKER}",
                "max_output_tokens": 16,
                "store": False,
                "stream": False,
            },
            proxies=proxies,
            timeout=(10, float(timeout)),
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        return {
            "decision": "retry",
            "code": "PROBE_NETWORK_ERROR",
            "status": 0,
            "error_type": type(exc).__name__,
        }

    status = int(response.status_code)
    # Cap body for classification only; do not persist or return.
    body = response.text[: 1024 * 1024]
    low = body.lower()

    if status == 200:
        try:
            data = response.json()
        except ValueError:
            return {"decision": "retry", "code": "INVALID_RESPONSE", "status": status}
        encoded = json.dumps(data, ensure_ascii=False)
        if data.get("status") == "completed" and MARKER in encoded:
            return {"decision": "pass", "code": "PROBE_PASSED", "status": status}
        return {"decision": "retry", "code": "INCOMPLETE_RESPONSE", "status": status}

    if status in (402, 429) or any(
        x in low
        for x in (
            "free-usage",
            "resource_exhausted",
            "quota",
            "spending-limit",
        )
    ):
        return {"decision": "cooldown", "code": "RATE_LIMITED", "status": status}

    if status == 401 or any(
        x in low for x in ("invalid_grant", "revok", "invalid credentials")
    ):
        return {"decision": "refresh", "code": "TOKEN_INVALID", "status": status}

    if "permission-denied" in low or "access to the chat endpoint is denied" in low:
        # Chat eligibility not ready yet — keep as retry/pending, not hard reject.
        return {"decision": "retry", "code": "PERMISSION_DENIED", "status": status}

    return {"decision": "retry", "code": "UPSTREAM_ERROR", "status": status}


def try_refresh_access_token(
    auth: dict[str, Any],
    *,
    proxy: str = "",
    timeout: float = 30,
) -> dict[str, Any]:
    """Best-effort refresh using project OAuth helper. Never logs tokens.

    Returns {ok, auth?, code, status}.
    """
    refresh = str(auth.get("refresh_token") or "").strip()
    if not refresh:
        return {"ok": False, "code": "MALFORMED_AUTH", "status": 0}

    try:
        import sys
        root = Path(__file__).resolve().parents[3]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from xconsole_client.xai_oauth import refresh_access_token  # type: ignore
    except Exception:
        return _refresh_with_requests(auth, proxy=proxy, timeout=timeout)

    try:
        token = refresh_access_token(refresh, proxy=proxy or None, timeout=timeout)
        if not isinstance(token, dict) or not token.get("access_token"):
            return {"ok": False, "code": "TOKEN_INVALID", "status": 0}
        new_auth = dict(auth)
        new_auth["access_token"] = token["access_token"]
        if token.get("refresh_token"):
            new_auth["refresh_token"] = token["refresh_token"]
        if token.get("id_token"):
            new_auth["id_token"] = token["id_token"]
        if token.get("expires_in") is not None:
            new_auth["expires_in"] = token.get("expires_in")
        return {"ok": True, "auth": new_auth, "code": "REFRESHED", "status": 200}
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        code = "TOKEN_INVALID" if ("revok" in msg or "invalid_grant" in msg) else "REFRESH_FAILED"
        return {"ok": False, "code": code, "status": 0, "error_type": type(exc).__name__}


def _refresh_with_requests(
    auth: dict[str, Any],
    *,
    proxy: str = "",
    timeout: float = 30,
) -> dict[str, Any]:
    refresh = str(auth.get("refresh_token") or "").strip()
    client_id = str(auth.get("client_id") or "app").strip() or "app"
    session = requests.Session()
    session.trust_env = False
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        resp = session.post(
            "https://auth.x.ai/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
            },
            headers={"Accept": "application/json"},
            proxies=proxies,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        return {
            "ok": False,
            "code": "PROBE_NETWORK_ERROR",
            "status": 0,
            "error_type": type(exc).__name__,
        }
    body = resp.text[:4000]
    low = body.lower()
    if resp.status_code != 200:
        code = "TOKEN_INVALID" if (
            resp.status_code == 401
            or "revok" in low
            or "invalid_grant" in low
        ) else "REFRESH_FAILED"
        return {"ok": False, "code": code, "status": resp.status_code}
    try:
        token = resp.json()
    except ValueError:
        return {"ok": False, "code": "INVALID_RESPONSE", "status": resp.status_code}
    access = str(token.get("access_token") or "").strip()
    if not access:
        return {"ok": False, "code": "TOKEN_INVALID", "status": resp.status_code}
    new_auth = dict(auth)
    new_auth["access_token"] = access
    if token.get("refresh_token"):
        new_auth["refresh_token"] = token["refresh_token"]
    if token.get("id_token"):
        new_auth["id_token"] = token["id_token"]
    if token.get("expires_in") is not None:
        new_auth["expires_in"] = token.get("expires_in")
    return {"ok": True, "auth": new_auth, "code": "REFRESHED", "status": resp.status_code}
