"""xAI OAuth device-code grant (Grok CLI).

Endpoints from https://auth.x.ai/.well-known/openid-configuration
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests as std_requests
from curl_cffi.const import CurlECode
from curl_cffi import requests as crequests
from curl_cffi.requests.exceptions import RequestException as CurlRequestException

from .proxyutil import resolve_proxy

# xAI OAuth client id (matches CLIProxyAPI default)
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
ISSUER = "https://auth.x.ai"
DEVICE_CODE_URL = "https://auth.x.ai/oauth2/device/code"
TOKEN_URL = "https://auth.x.ai/oauth2/token"
SCOPE = "openid profile email offline_access grok-cli:access api:access"

LogFn = Callable[[str], None]


def _noop_log(_: str) -> None:
    return None


def _post_form(
    url: str,
    form: dict[str, str],
    timeout: float = 30.0,
    *,
    proxy: str | None = None,
) -> tuple[int, dict[str, Any] | str]:
    resolved_proxy = resolve_proxy(proxy)
    proxies = {"http": resolved_proxy, "https": resolved_proxy} if resolved_proxy else None
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": "grok-reg-oidc-minter/1.0",
    }
    try:
        response = crequests.post(
            url,
            data=form,
            headers=headers,
            timeout=timeout,
            proxies=proxies,
            impersonate="chrome",
        )
    except CurlRequestException as exc:
        if exc.code != CurlECode.SSL_CONNECT_ERROR:
            raise
        # curl_cffi intermittently fails TLS handshakes under Windows concurrency.
        # Keep the same proxy and certificate verification while changing transport.
        with std_requests.Session() as session:
            session.trust_env = False
            response = session.post(
                url,
                data=form,
                headers=headers,
                timeout=timeout,
                proxies=proxies,
            )
    try:
        return int(response.status_code), response.json()
    except (json.JSONDecodeError, ValueError):
        return int(response.status_code), response.text


@dataclass
class DeviceCodeSession:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int
    raw: dict[str, Any]


@dataclass
class TokenResult:
    access_token: str
    refresh_token: str
    id_token: str | None
    token_type: str
    expires_in: int
    raw: dict[str, Any]


class OAuthDeviceError(RuntimeError):
    pass


def request_device_code(
    *,
    client_id: str = CLIENT_ID,
    scope: str = SCOPE,
    timeout: float = 30.0,
    proxy: str | None = None,
) -> DeviceCodeSession:
    status, body = _post_form(
        DEVICE_CODE_URL,
        {"client_id": client_id, "scope": scope},
        timeout=timeout,
        proxy=proxy,
    )
    if status != 200 or not isinstance(body, dict):
        raise OAuthDeviceError(f"device code request failed HTTP {status}: {body!r}")
    device_code = str(body.get("device_code") or "").strip()
    user_code = str(body.get("user_code") or "").strip()
    if not device_code or not user_code:
        raise OAuthDeviceError(f"device code response missing fields: {body}")
    vuri = str(body.get("verification_uri") or "https://accounts.x.ai/oauth2/device").strip()
    vcomplete = str(
        body.get("verification_uri_complete") or f"{vuri}?user_code={user_code}"
    ).strip()
    expires_in = int(body.get("expires_in") or 1800)
    interval = max(int(body.get("interval") or 5), 1)
    return DeviceCodeSession(
        device_code=device_code,
        user_code=user_code,
        verification_uri=vuri,
        verification_uri_complete=vcomplete,
        expires_in=expires_in,
        interval=interval,
        raw=body,
    )


def poll_device_token(
    device_code: str,
    *,
    client_id: str = CLIENT_ID,
    interval: int = 5,
    expires_in: int = 1800,
    timeout: float = 30.0,
    log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
    proxy: str | None = None,
) -> TokenResult:
    """Poll token endpoint until authorized or expired."""
    log = log or _noop_log
    deadline = time.time() + max(expires_in - 5, 30)
    sleep_for = max(interval, 1)
    while time.time() < deadline:
        if cancel and cancel():
            raise OAuthDeviceError("cancelled")
        status, body = _post_form(
            TOKEN_URL,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": client_id,
            },
            timeout=timeout,
            proxy=proxy,
        )
        if status == 200 and isinstance(body, dict) and body.get("access_token"):
            access = str(body["access_token"]).strip()
            refresh = str(body.get("refresh_token") or "").strip()
            if not refresh:
                raise OAuthDeviceError("token response missing refresh_token")
            return TokenResult(
                access_token=access,
                refresh_token=refresh,
                id_token=(str(body["id_token"]).strip() if body.get("id_token") else None),
                token_type=str(body.get("token_type") or "Bearer"),
                expires_in=int(body.get("expires_in") or 21600),
                raw=body,
            )
        err = ""
        desc = ""
        if isinstance(body, dict):
            err = str(body.get("error") or "")
            desc = str(body.get("error_description") or "")
        if err in ("authorization_pending", "slow_down"):
            if err == "slow_down":
                sleep_for = min(sleep_for + 5, 30)
            log(f"oauth poll: {err} (sleep {sleep_for}s)")
            time.sleep(sleep_for)
            continue
        if err in ("expired_token", "access_denied"):
            raise OAuthDeviceError(f"device auth failed: {err}: {desc}")
        if status == 400 and err:
            raise OAuthDeviceError(f"device auth token error: {err}: {desc or body}")
        log(f"oauth poll unexpected HTTP {status}: {body!r}")
        time.sleep(sleep_for)
    raise OAuthDeviceError("device auth timed out waiting for user approval")
