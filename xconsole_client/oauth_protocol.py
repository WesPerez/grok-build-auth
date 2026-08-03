# -*- coding: utf-8 -*-
"""Protocolized xAI OAuth login (no browser) for Grok Build / Sub2API.

After account signup (or with email/password), this module:

  1. Starts OAuth PKCE against auth.x.ai
  2. Lands on accounts.x.ai/sign-in?redirect=oauth2-provider&return_to=/oauth2/consent?...
  3. Solves Cloudflare Turnstile via YesCaptcha
  4. Calls auth_mgmt.AuthManagement/CreateSession (gRPC-web)
  5. Follows cookieSetterUrl + OAuth redirects to capture authorization code
  6. Exchanges code for tokens and exports Sub2API Grok Build auth JSON

CreateSessionRequest wire layout (reverse-engineered 2026-07):

  field 1  Credentials {
      field 1  EmailAndPassword { email=1, clearTextPassword=2 }
  }
  field 4  AntiAbuseToken {
      field 1  turnstileToken
      field 2  castleRequestToken (optional, may be empty)
  }
"""
from __future__ import annotations

import re
import secrets
from html.parser import HTMLParser
import json
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse

from . import grpcweb
from .solver import YesCaptchaSolver
from .security import redact_text, sanitize_url
from .xai_oauth import (
    AUTHORIZATION_ENDPOINT,
    CLIPROXYAPI_GROK_BASE_URL,
    DEFAULT_CLIENT_ID,
    DEFAULT_SCOPES,
    OAuthLoginResult,
    TOKEN_ENDPOINT,
    _finalize_oauth_code,
    build_authorization_url,
    code_challenge_s256,
    generate_code_verifier,
)

TURNSTILE_SITEKEY = "0x4AAAAAAAhr9JGVDZbrZOo0"
CREATE_SESSION_RPC = "https://accounts.x.ai/auth_mgmt.AuthManagement/CreateSession"
CREATE_COOKIE_SETTER_RPC = "https://accounts.x.ai/auth_mgmt.AuthManagement/CreateCookieSetterLink"
ACCOUNTS_ORIGIN = "https://accounts.x.ai"
SESSION_COOKIE_NAMES = frozenset({"sso", "sso-rw", "sso_jwt", "cf_clearance", "__cf_bm"})
SESSION_COOKIE_DOMAINS = (".x.ai", "accounts.x.ai", "auth.x.ai")
# Observed Next.js server action for the consent Allow button (may change on deploy).
SUBMIT_OAUTH2_CONSENT_ACTION = "4005315a1d7e426de592990bb54bb37471f39dd6d2"


class _ConsentFormParser(HTMLParser):
    _FIELDS = frozenset({
        "client_id",
        "redirect_uri",
        "scope",
        "state",
        "code_challenge",
        "code_challenge_method",
        "nonce",
        "principal_type",
        "principal_id",
        "referrer",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}
        self.script_chunks: list[str] = []
        self.deployment_id = ""
        self._inside_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() == "html":
            values = {str(key).lower(): "" if value is None else str(value) for key, value in attrs}
            self.deployment_id = values.get("data-dpl-id", "")
            return
        if tag.lower() == "script":
            self._inside_script = True
            return
        if tag.lower() != "input":
            return
        values = {str(key).lower(): "" if value is None else str(value) for key, value in attrs}
        name = values.get("name", "")
        if name in self._FIELDS:
            self.values[name] = values.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script":
            self._inside_script = False

    def handle_data(self, data: str) -> None:
        if self._inside_script and "self.__next_f.push(" in data:
            self.script_chunks.append(data)


def _flight_strings(script_chunks: list[str]) -> list[str]:
    values: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)

    marker = "self.__next_f.push("
    decoder = json.JSONDecoder()
    for script in script_chunks:
        offset = 0
        while True:
            start = script.find(marker, offset)
            if start < 0:
                break
            start += len(marker)
            try:
                value, consumed = decoder.raw_decode(script[start:])
            except json.JSONDecodeError:
                offset = start
                continue
            collect(value)
            offset = start + consumed
    return values


def extract_consent_form_values(page_html: str) -> dict[str, str]:
    parser = _ConsentFormParser()
    parser.feed(page_html or "")
    parser.close()
    values = dict(parser.values)
    key_map = {
        "clientId": "client_id",
        "redirectUri": "redirect_uri",
        "scope": "scope",
        "state": "state",
        "codeChallenge": "code_challenge",
        "codeChallengeMethod": "code_challenge_method",
        "nonce": "nonce",
        "principalType": "principal_type",
        "principalId": "principal_id",
        "referrer": "referrer",
        "principal_type": "principal_type",
        "principal_id": "principal_id",
    }
    for chunk in _flight_strings(parser.script_chunks):
        variants = [chunk]
        for _ in range(2):
            normalized = variants[-1].replace('\\"', '"').replace('\\\\', '\\')
            if normalized == variants[-1]:
                break
            variants.append(normalized)
        for variant in variants:
            for source, target in key_map.items():
                if values.get(target):
                    continue
                match = re.search(rf'"{re.escape(source)}"\s*:\s*"([^"\\]*)"', variant)
                if match and match.group(1):
                    values[target] = match.group(1)
    return values


def consent_principal_is_valid(principal_type: str, principal_id: str) -> bool:
    normalized = (principal_type or "").strip()
    return bool(normalized and (normalized.lower() == "user" or (principal_id or "").strip()))


def _prepare_flight_router_state(tree: Any) -> list[Any]:
    """Mirror Next.js prepareFlightRouterStateForRequest for JSON Flight data."""
    if not isinstance(tree, list) or len(tree) < 2 or not isinstance(tree[1], dict):
        raise ValueError("invalid Flight router state")

    segment = tree[0]
    if isinstance(segment, str):
        if segment.startswith("__PAGE__?"):
            segment = "__PAGE__"
    elif isinstance(segment, list) and len(segment) >= 3:
        segment = [segment[0], segment[1], segment[2], None]
    else:
        raise ValueError("invalid Flight router segment")

    children = {
        str(key): _prepare_flight_router_state(value)
        for key, value in tree[1].items()
    }
    prepared: list[Any] = [segment, children]

    refresh = tree[3] if len(tree) > 3 else "$undefined"
    if refresh not in (None, False, "", 0, "$undefined"):
        prepared.extend([None, refresh])

    if len(tree) > 4 and tree[4] != "$undefined":
        while len(prepared) < 4:
            prepared.append(None)
        prepared.append(tree[4])
    return prepared


def extract_consent_router_state_tree(page_html: str) -> str:
    """Return the URL-encoded router tree used by the current consent page."""
    parser = _ConsentFormParser()
    parser.feed(page_html or "")
    parser.close()
    decoder = json.JSONDecoder()

    for chunk in _flight_strings(parser.script_chunks):
        variants = [chunk]
        for _ in range(2):
            normalized = variants[-1].replace('\\"', '"').replace('\\\\', '\\')
            if normalized == variants[-1]:
                break
            variants.append(normalized)
        for variant in variants:
            for match in re.finditer(r'"f"\s*:\s*', variant):
                try:
                    flight, _ = decoder.raw_decode(variant[match.end():])
                    tree = flight[0][0]
                    prepared = _prepare_flight_router_state(tree)
                except (IndexError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                compact = json.dumps(prepared, ensure_ascii=False, separators=(",", ":"))
                return quote(compact, safe="")
    return ""


def extract_next_deployment_id(page_html: str) -> str:
    parser = _ConsentFormParser()
    parser.feed(page_html or "")
    parser.close()
    value = parser.deployment_id.strip()
    return value if re.fullmatch(r"[A-Za-z0-9._-]{1,200}", value) else ""


def build_consent_action_request(
    *,
    page_url: str,
    action_id: str,
    router_state_tree: str,
    action_args: list[Any],
    deployment_id: str = "",
) -> tuple[str, dict[str, str], bytes]:
    """Build the browser-equivalent Next.js Server Action request."""
    parsed = urlparse(page_url)
    if parsed.scheme != "https" or parsed.hostname != "accounts.x.ai":
        raise ValueError("consent action target must be accounts.x.ai over HTTPS")
    if parsed.path.rstrip("/") != "/oauth2/consent":
        raise ValueError("consent action target path is invalid")
    if not re.fullmatch(r"[a-f0-9]{40,44}", action_id, re.I):
        raise ValueError("consent action ID is invalid")
    if not router_state_tree:
        raise ValueError("consent router state is missing")

    canonical_url = parsed._replace(fragment="").geturl()
    body = json.dumps(action_args, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "accept": "text/x-component",
        "content-type": "text/plain;charset=UTF-8",
        "next-action": action_id,
        "next-router-state-tree": router_state_tree,
        "origin": ACCOUNTS_ORIGIN,
        "referer": canonical_url,
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }
    if deployment_id:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", deployment_id):
            raise ValueError("Next.js deployment ID is invalid")
        headers["x-deployment-id"] = deployment_id
    return canonical_url, headers, body


def extract_consent_action_error(response_text: str) -> str:
    variants = [response_text or ""]
    for _ in range(3):
        normalized = variants[-1].replace('\\"', '"').replace('\\\\', '\\')
        if normalized == variants[-1]:
            break
        variants.append(normalized)
    for variant in variants:
        match = re.search(r'"error"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', variant)
        if not match:
            continue
        value = match.group(1)
        try:
            return json.loads(f'"{value}"')
        except json.JSONDecodeError:
            return value
    return ""


def classify_consent_action_error(server_error: str) -> str:
    response_low = (server_error or "").lower()
    if any(word in response_low for word in ("unauthorized", "unauthenticated", "sign in", "session")):
        return "session"
    if "access denied" in response_low or "authorization denied" in response_low:
        return "authorization"
    if "principal" in response_low:
        return "principal"
    if "scope" in response_low:
        return "scope"
    if "redirect" in response_low:
        return "redirect"
    if "client" in response_low:
        return "client"
    if "action" in response_low or "csrf" in response_low:
        return "action"
    return "unclassified"


def _enc_msg(field_no: int, raw: bytes) -> bytes:
    return grpcweb.encode_bytes(field_no, raw)


def encode_create_session_request(
    email: str,
    password: str,
    *,
    turnstile_token: str,
    castle_request_token: str = "",
) -> bytes:
    """Encode CreateSessionRequest protobuf body."""
    email_pw = grpcweb.encode_string(1, email) + grpcweb.encode_string(2, password)
    # Credentials.credentials oneof emailAndPassword = field 1
    credentials = _enc_msg(1, email_pw)
    # CreateSessionRequest.credentials = field 1
    req = _enc_msg(1, credentials)
    # CreateSessionRequest.anti_abuse_token = field 4
    anti = grpcweb.encode_string(1, turnstile_token)
    if castle_request_token:
        anti += grpcweb.encode_string(2, castle_request_token)
    else:
        anti += grpcweb.encode_string(2, "")
    req += _enc_msg(4, anti)
    return req


def _grpc_headers(referer: str) -> Dict[str, str]:
    return {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "accept": "*/*",
        "origin": ACCOUNTS_ORIGIN,
        "referer": referer,
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }


def _extract_urls_from_fields(fields: List[Dict[str, Any]]) -> List[str]:
    urls: List[str] = []
    for f in fields:
        if f.get("type") == "string":
            val = str(f.get("value") or "")
            if val.startswith("http://") or val.startswith("https://"):
                urls.append(val)
        elif f.get("type") == "bytes" and f.get("hex"):
            try:
                raw = bytes.fromhex(f["hex"])
                nested = grpcweb.decode_message(raw)
                urls.extend(_extract_urls_from_fields(nested))
            except Exception:
                pass
    return urls


def _parse_grpc_error(headers: Dict[str, str], body: bytes) -> Tuple[Optional[int], str]:
    # Trailers may be in body frames or HTTP headers (connect/grpc-web).
    status = headers.get("grpc-status")
    message = unquote(headers.get("grpc-message") or "")
    if status is not None:
        try:
            return int(status), message
        except ValueError:
            return None, message
    try:
        parsed = grpcweb.parse_response(body)
    except Exception:
        return None, message
    if parsed.get("grpc_status") is not None:
        return int(parsed["grpc_status"]), message or str(parsed.get("trailers") or "")
    return None, message


def extract_cookies_from_auth_client(client: Any) -> Dict[str, str]:
    """Best-effort dump of name->value cookies from XConsoleAuthClient."""
    out: Dict[str, str] = {}
    try:
        jar = client._t.cookies  # type: ignore[attr-defined]
    except Exception:
        return out
    # dict-like
    try:
        if hasattr(jar, "items"):
            for k, v in jar.items():
                if k and v is not None:
                    out[str(k)] = str(v)
            if out:
                return out
    except Exception:
        pass
    # curl_cffi jar iteration
    try:
        iterable = jar.jar if hasattr(jar, "jar") else jar
        for ck in iterable:
            name = getattr(ck, "name", None)
            value = getattr(ck, "value", None)
            if name and value is not None:
                out[str(name)] = str(value)
    except Exception:
        pass
    return out


class ProtocolOAuthClient:
    """HTTP-only OAuth client using curl_cffi fingerprint + YesCaptcha."""

    def __init__(
        self,
        *,
        yescaptcha_key: str = "",
        proxy: str = "",
        impersonate: str = "chrome131",
        debug: bool = False,
        turnstile_premium: bool = True,
    ):
        self.debug = debug
        self.turnstile_premium = turnstile_premium
        self._yescaptcha_key = (yescaptcha_key or "").strip()
        self.solver: Optional[YesCaptchaSolver] = None
        if self._yescaptcha_key:
            self.solver = YesCaptchaSolver(self._yescaptcha_key, debug=debug)
        try:
            from curl_cffi import requests as creq
        except ImportError as exc:
            raise RuntimeError("curl_cffi is required for protocol OAuth") from exc
        kwargs: Dict[str, Any] = {"impersonate": impersonate}
        if proxy:
            kwargs["proxies"] = {"http": proxy, "https": proxy}
        self._s = creq.Session(**kwargs)

    def load_cookies(self, cookies) -> None:
        """Inject pre-existing accounts.x.ai session cookies (e.g. post-signup).

        Accepts:
          - Dict[str, str]: name -> value
          - list[dict]: Playwright-style [{name: ..., value: ...}, ...]
        """
        if not cookies:
            return
        cookie_dict: Dict[str, str] = {}
        if isinstance(cookies, list):
            for c in cookies:
                n = c.get("name") if isinstance(c, dict) else getattr(c, "name", None)
                v = c.get("value") if isinstance(c, dict) else getattr(c, "value", None)
                if n and v is not None:
                    cookie_dict[str(n)] = str(v)
        elif isinstance(cookies, dict):
            cookie_dict = {str(k): str(v) for k, v in cookies.items() if v is not None}
        else:
            return
        loaded_names: set[str] = set()
        for name, value in cookie_dict.items():
            if name not in SESSION_COOKIE_NAMES:
                continue
            for domain in SESSION_COOKIE_DOMAINS:
                try:
                    self._s.cookies.set(name, value, domain=domain)
                except Exception:
                    pass
            loaded_names.add(name)
        self._log(f"loaded allowlisted session cookies: {sorted(loaded_names)}")

    def _log(self, msg: str) -> None:
        if self.debug:
            print(f"  [oauth-protocol] {redact_text(msg)}")

    def _get(self, url: str, *, allow_redirects: bool = True, headers: Optional[Dict[str, str]] = None):
        h = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "upgrade-insecure-requests": "1",
        }
        if headers:
            h.update(headers)
        return self._s.get(url, headers=h, allow_redirects=allow_redirects, timeout=45)

    def _set_sso_cookie(self, jwt_token: str) -> None:
        """Attach the session JWT to the xAI auth hosts used during OAuth."""
        if not jwt_token:
            return
        for name in ("sso", "sso-rw"):
            for domain in SESSION_COOKIE_DOMAINS:
                try:
                    self._s.cookies.set(name, jwt_token, domain=domain)
                except Exception:
                    pass

    @staticmethod
    def _find_consent_action_id(source: str) -> Optional[str]:
        """Find the server action bound to submitOAuth2Consent in HTML or JS."""
        if not source or "submitOAuth2Consent" not in source:
            return None
        patterns = (
            r'createServerReference\)\("([a-f0-9]{40,44})"[^)]{0,500}submitOAuth2Consent',
            r'"([a-f0-9]{40,44})".{0,500}submitOAuth2Consent',
            r'submitOAuth2Consent.{0,500}"([a-f0-9]{40,44})"',
        )
        for pattern in patterns:
            match = re.search(pattern, source, re.I | re.S)
            if match:
                return match.group(1)
        return None

    def _resolve_consent_action_id(self, page_url: str, page_html: str) -> str:
        """Resolve the deployment-specific consent action from live Next.js assets."""
        inline = self._find_consent_action_id(page_html)
        if inline:
            self._log(f"consent action resolved from HTML: {inline[:16]}...")
            return inline

        script_urls: list[str] = []
        seen: set[str] = set()
        for raw in re.findall(r'<script[^>]+src=["\']([^"\']+\.js(?:\?[^"\']*)?)["\']', page_html, re.I):
            url = urljoin(page_url, raw.replace("&amp;", "&"))
            if url in seen:
                continue
            seen.add(url)
            script_urls.append(url)
        self._log(f"searching {len(script_urls)} consent JS chunks")
        for url in script_urls:
            self._log(f"consent script candidate {sanitize_url(url)}")

        for url in script_urls:
            try:
                response = self._get(url, headers={"accept": "*/*"})
            except Exception:
                continue
            if response.status_code != 200:
                continue
            action_id = self._find_consent_action_id(response.text or "")
            if action_id:
                self._log(
                    f"consent action resolved from {sanitize_url(url)}: {action_id[:16]}..."
                )
                return action_id

        self._log("live consent action was not found; using compatibility fallback")
        return SUBMIT_OAUTH2_CONSENT_ACTION

    def create_cookie_setter_link(
        self,
        success_url: str,
        *,
        error_url: str = f"{ACCOUNTS_ORIGIN}/sign-in",
        referer: str = f"{ACCOUNTS_ORIGIN}/sign-in",
    ) -> Dict[str, Any]:
        """Call CreateCookieSetterLink; returns cookie_setter_url for the multi-domain hop."""
        msg = grpcweb.encode_string(1, success_url) + grpcweb.encode_string(2, error_url)
        resp = self._s.post(
            CREATE_COOKIE_SETTER_RPC,
            headers=_grpc_headers(referer),
            data=grpcweb.frame_request(msg),
            timeout=45,
        )
        hdrs = {k.lower(): v for k, v in resp.headers.items()}
        header_status, header_msg = _parse_grpc_error(hdrs, resp.content)
        try:
            parsed = grpcweb.parse_response(resp.content)
        except Exception:
            parsed = {"messages": [], "trailers": {}, "grpc_status": None}
        grpc_status = parsed.get("grpc_status")
        if grpc_status is None:
            grpc_status = header_status
        grpc_msg = header_msg or unquote(str((parsed.get("trailers") or {}).get("grpc-message") or ""))
        fields = parsed["messages"][0] if parsed.get("messages") else []
        urls = _extract_urls_from_fields(fields)
        cookie_setter = next((u for u in urls if "set-cookie" in u), None) or (urls[0] if urls else None)
        ok = grpc_status in (None, 0) and bool(cookie_setter)
        return {
            "ok": ok,
            "error": None if ok else (grpc_msg or "CreateCookieSetterLink failed"),
            "grpc_status": grpc_status,
            "cookie_setter_url": cookie_setter,
            "raw_fields": fields,
        }

    def create_session(self, email: str, password: str, *, referer: str) -> Dict[str, Any]:
        """Call CreateSession; on success stores sso JWT on the session.

        CreateSession field 2 is a session JWT (not the cookie-setter URL).
        Call :meth:`create_cookie_setter_link` next with the OAuth consent URL.
        """
        if not self.solver:
            return {
                "ok": False,
                "error": "YESCAPTCHA_API_KEY required for CreateSession Turnstile",
                "grpc_status": None,
                "session_jwt": None,
                "raw_fields": [],
            }
        self._log("solving Turnstile for sign-in...")
        turnstile = self.solver.solve_turnstile(
            website_url=referer.split("#")[0],
            website_key=TURNSTILE_SITEKEY,
            premium=self.turnstile_premium,
        )
        self._log(f"Turnstile {len(turnstile)} chars")

        body = encode_create_session_request(
            email, password, turnstile_token=turnstile, castle_request_token=""
        )
        framed = grpcweb.frame_request(body)
        resp = self._s.post(
            CREATE_SESSION_RPC,
            headers=_grpc_headers(referer),
            data=framed,
            timeout=45,
        )
        hdrs = {k.lower(): v for k, v in resp.headers.items()}
        header_status, header_msg = _parse_grpc_error(hdrs, resp.content)
        try:
            parsed = grpcweb.parse_response(resp.content)
        except Exception:
            parsed = {"messages": [], "trailers": {}, "grpc_status": None}

        grpc_status = parsed.get("grpc_status")
        if grpc_status is None:
            grpc_status = header_status
        grpc_msg = header_msg
        if not grpc_msg and parsed.get("trailers"):
            grpc_msg = unquote(str(parsed["trailers"].get("grpc-message") or ""))

        fields = parsed["messages"][0] if parsed.get("messages") else []
        session_jwt = None
        for f in fields:
            if f.get("type") == "string":
                val = str(f.get("value") or "")
                if val.startswith("eyJ") and val.count(".") >= 2:
                    session_jwt = val
                    break

        if grpc_status not in (None, 0) or not session_jwt:
            return {
                "ok": False,
                "error": grpc_msg or (
                    f"CreateSession failed (status={grpc_status}, fields={len(fields)})"
                ),
                "grpc_status": grpc_status,
                "session_jwt": session_jwt,
                "raw_fields": fields,
            }

        self._set_sso_cookie(session_jwt)
        self._log("CreateSession OK")
        return {
            "ok": True,
            "error": None,
            "grpc_status": 0 if grpc_status is None else grpc_status,
            "session_jwt": session_jwt,
            "raw_fields": fields,
        }

    @staticmethod
    def _absolute_return_to(url: str) -> Optional[str]:
        """Extract absolute return_to target from a sign-in URL."""
        qs = parse_qs(urlparse(url).query)
        rt = (qs.get("return_to") or [""])[0]
        if not rt:
            return None
        rt = unquote(rt)
        if rt.startswith("/"):
            return ACCOUNTS_ORIGIN + rt
        if rt.startswith("http://") or rt.startswith("https://"):
            return rt
        return urljoin(ACCOUNTS_ORIGIN + "/", rt)

    def _follow_for_code(
        self,
        start_url: str,
        *,
        redirect_uri: str,
        state: str,
        max_hops: int = 25,
    ) -> str:
        """Follow redirects / cookie-setter until redirect_uri?code=... is reached."""
        current = start_url
        pending_return_to: Optional[str] = None
        visited: set[str] = set()

        for hop in range(max_hops):
            self._log(f"hop {hop}: {current[:160]}")
            # Never let the HTTP client connect to localhost callback.
            if current.startswith(redirect_uri) or (
                "code=" in current and "state=" in current and "127.0.0.1" in current
            ):
                return self._code_from_url(current, state)

            # Remember OAuth return_to while we bounce through sign-in.
            rt = self._absolute_return_to(current)
            if rt:
                pending_return_to = rt

            # If a hop dumps us on /account while OAuth return_to is known, recover.
            # Do NOT auto-jump from /sign-in (that can trigger sign-out loops).
            path = urlparse(current).path or ""
            if pending_return_to and path.rstrip("/") in ("/account", "/home"):
                key = "rt:" + pending_return_to
                if key not in visited:
                    visited.add(key)
                    self._log(f"account page → return_to {pending_return_to[:140]}")
                    current = pending_return_to
                    continue

            if current in visited and hop > 2:
                raise RuntimeError(f"OAuth redirect loop at {current[:180]}")
            visited.add(current)

            resp = self._get(current, allow_redirects=False)
            status = resp.status_code
            loc = resp.headers.get("location") or resp.headers.get("Location")

            if status in (301, 302, 303, 307, 308) and loc:
                nxt = urljoin(current, loc)
                if nxt.startswith(redirect_uri) or (
                    "code=" in nxt and ("127.0.0.1" in nxt or "localhost" in nxt)
                ):
                    return self._code_from_url(nxt, state)
                # sign-in → /account while we still have return_to: go to consent
                nxt_path = urlparse(nxt).path or ""
                if pending_return_to and nxt_path.rstrip("/") in ("/account", "/home"):
                    self._log("redirect to account intercepted; using return_to")
                    current = pending_return_to
                    continue
                current = nxt
                continue

            # HTML page: try meta-refresh / JS location / form action
            html = resp.text or ""
            m2 = re.search(
                r'https?://127\.0\.0\.1[^\"\'\s<>]*code=[^\"\'\s<>]+',
                html,
            )
            if m2:
                return self._code_from_url(m2.group(0).replace("&amp;", "&"), state)

            m = re.search(
                r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+url=([^\"\'>\s]+)',
                html,
                re.I,
            )
            if m:
                current = urljoin(current, unquote(m.group(1)))
                continue

            # Consent page: look for authorize/continue links or form actions
            for pat in (
                r'href=["\']([^"\']*oauth2[^"\']*)["\']',
                r'action=["\']([^"\']*oauth2[^"\']*)["\']',
                r'href=["\']([^"\']*callback[^"\']*)["\']',
            ):
                m = re.search(pat, html, re.I)
                if m:
                    candidate = urljoin(current, m.group(1).replace("&amp;", "&"))
                    if candidate != current and candidate not in visited:
                        current = candidate
                        break
            else:
                # If consent URL itself is the current page and already logged in,
                # try POST approve is unknown; last resort: re-hit return_to once.
                if pending_return_to and current != pending_return_to and pending_return_to not in visited:
                    current = pending_return_to
                    continue
                raise RuntimeError(
                    f"OAuth redirect chain stalled at HTTP {status} {current[:180]} "
                    f"(no authorization code)."
                )
            continue

        raise TimeoutError("OAuth redirect chain exceeded max hops without code")

    @staticmethod
    def _code_from_url(url: str, expected_state: str) -> str:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        if qs.get("error"):
            detail = (qs.get("error_description") or qs.get("error") or [""])[0]
            raise RuntimeError(f"authorization failed: {detail}")
        got_state = (qs.get("state") or [""])[0]
        if got_state and got_state != expected_state:
            raise RuntimeError("authorization failed: state mismatch")
        code = (qs.get("code") or [""])[0]
        if not code:
            raise RuntimeError(f"authorization failed: missing code in {url[:200]}")
        return code

    def _code_from_consent_redirect(
        self,
        response: Any,
        *,
        page_url: str,
        redirect_uri: str,
        state: str,
    ) -> Optional[str]:
        """Resolve a consent action Location without assuming it is the callback.

        Next.js server actions may return an intermediate 303 before the OAuth
        callback.  The old path only accepted a Location that already contained
        ``code=`` and incorrectly failed otherwise.
        """
        action_redirect = (
            response.headers.get("x-action-redirect")
            or response.headers.get("X-Action-Redirect")
            or ""
        )
        location = action_redirect.split(";", 1)[0] if action_redirect else (
            response.headers.get("location") or response.headers.get("Location") or ""
        )
        if not location:
            return None
        target = urljoin(page_url, location)
        if "code=" in target:
            return self._code_from_url(target, state)
        return self._follow_for_code(target, redirect_uri=redirect_uri, state=state)

    def login(
        self,
        email: str,
        password: str,
        *,
        client_id: str = DEFAULT_CLIENT_ID,
        scopes: Optional[List[str]] = None,
        redirect_host: str = "127.0.0.1",
        redirect_port: int = 56121,
        output_dir: Optional[str] = None,
        cliproxyapi_auth_dir: Optional[str] = None,
        cliproxyapi_base_url: str = CLIPROXYAPI_GROK_BASE_URL,
        cliproxyapi_disabled: bool = False,
        proxy: str = "",
        session_cookies: Optional[Dict[str, str]] = None,
    ) -> OAuthLoginResult:
        scopes = scopes or list(DEFAULT_SCOPES)
        if session_cookies:
            self.load_cookies(session_cookies)

        state = secrets.token_hex(16)
        nonce = secrets.token_hex(16)
        verifier = generate_code_verifier()
        challenge = code_challenge_s256(verifier)
        redirect_uri = f"http://{redirect_host}:{int(redirect_port)}/callback"

        auth_url = build_authorization_url(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            nonce=nonce,
            code_challenge=challenge,
            scopes=scopes,
        )
        # Consent URL is on the CreateCookieSetterLink allowlist (authorize URL is not).
        consent_url = (
            f"{ACCOUNTS_ORIGIN}/oauth2/consent?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "scope": " ".join(scopes),
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "nonce": nonce,
                }
            )
        )

        def _apply_set_cookie_url(setter_url: str) -> str:
            """GET set-cookie hop, apply JWT token as sso, return next success_url."""
            from .sso import parse_jwt_payload, _extract_jwt_from_url

            jwt = _extract_jwt_from_url(setter_url) or ""
            payload = parse_jwt_payload(jwt) if jwt else None
            cfg = (payload or {}).get("config") if isinstance(payload, dict) else None
            token = ""
            success = ""
            if isinstance(cfg, dict):
                token = str(cfg.get("token") or "")
                success = str(cfg.get("success_url") or "")
            if token:
                self._set_sso_cookie(token)
                self._log("applied allowlisted set-cookie token")
            # Hit the set-cookie endpoint so domain cookies are written.
            resp = self._get(setter_url, allow_redirects=False)
            loc = resp.headers.get("location") or resp.headers.get("Location") or ""
            if loc:
                nxt = urljoin(setter_url, loc)
                self._log(f"set-cookie Location → {nxt[:160]}")
                return nxt
            if success:
                return success
            return str(resp.url)

        def _submit_oauth2_consent(page_url: str, page_html: str = "") -> str:
            """POST Next.js submitOAuth2Consent server action; return authorization code."""
            action_id = self._resolve_consent_action_id(page_url, page_html)
            form = extract_consent_form_values(page_html)
            self._log(f"consent fields: {','.join(sorted(form)) or 'none'}")
            principal_type = str(form.get("principal_type") or "").strip()
            principal_id = str(form.get("principal_id") or "").strip()
            if not consent_principal_is_valid(principal_type, principal_id):
                raise RuntimeError("consent page is missing its principal identity")
            selected_scope = str(form.get("scope") or " ".join(scopes)).strip()
            scope_items = selected_scope.split()
            self._log(
                "consent value profile: "
                f"principal_type={principal_type} "
                f"principal_id_present={bool(principal_id)} "
                f"scope_count={len(scope_items)} "
                f"scope_matches_default={scope_items == list(scopes)} "
                f"referrer_present={bool(str(form.get('referrer') or '').strip())}"
            )

            router_state_tree = extract_consent_router_state_tree(page_html)
            if not router_state_tree:
                raise RuntimeError("consent page is missing its live Flight router state")
            deployment_id = extract_next_deployment_id(page_html)
            self._log(
                "consent request runtime: "
                "router_state=flight "
                f"deployment_id_present={bool(deployment_id)} next_url_present=False"
            )

            action_args = [{
                "action": "allow",
                "clientId": form.get("client_id") or client_id,
                "redirectUri": form.get("redirect_uri") or redirect_uri,
                "scope": selected_scope,
                "state": form.get("state") or state,
                "codeChallenge": form.get("code_challenge") or challenge,
                "codeChallengeMethod": form.get("code_challenge_method") or "S256",
                "nonce": form.get("nonce") or nonce,
                "principalType": principal_type,
                "principalId": principal_id,
                "referrer": form.get("referrer") or "",
            }]
            target_url, headers, body = build_consent_action_request(
                page_url=page_url,
                action_id=action_id,
                router_state_tree=router_state_tree,
                action_args=action_args,
                deployment_id=deployment_id,
            )
            self._log(f"submitOAuth2Consent action={action_id[:16]}...")
            # Next.js posts once to the current canonical URL, including OAuth
            # query parameters. Replaying a Server Action can duplicate effects.
            resp = self._s.post(target_url, headers=headers, data=body, timeout=45)
            text = resp.text or ""
            has_code = bool(re.search(r'"code"\s*:\s*"[^"]+"|code=[A-Za-z0-9._~\-]+', text))
            has_success = bool(re.search(r'"success"\s*:\s*true', text, re.I))
            self._log(
                f"consent action HTTP {resp.status_code} "
                f"success={has_success} code_present={has_code}"
            )
            if not has_success and not has_code:
                server_error = extract_consent_action_error(text)
                if server_error:
                    self._log(f"consent server error: {redact_text(server_error)[:180]}")
                error_kind = classify_consent_action_error(server_error)
                self._log(f"consent response error kind={error_kind}")
            # Response may be RSC flight text containing JSON with code.
            m = re.search(r'"code"\s*:\s*"([^"]+)"', text)
            if m:
                return m.group(1)
            m = re.search(r'code=([A-Za-z0-9._~\-]+)', text)
            if m and "error" not in m.group(0):
                return m.group(1)
            # Or redirect header
            redirected_code = self._code_from_consent_redirect(
                resp,
                page_url=page_url,
                redirect_uri=redirect_uri,
                state=state,
            )
            if redirected_code:
                return redirected_code
            raise RuntimeError(f"submitOAuth2Consent failed HTTP {resp.status_code}: {text[:300]}")

        def _complete_via_cookie_setter(label: str) -> str:
            """Mint set-cookie chain with consent as success_url, then Allow consent."""
            # Prime authorize so the AS has a pending OAuth request.
            self._get(auth_url, allow_redirects=False)
            csl = self.create_cookie_setter_link(
                consent_url,
                error_url=f"{ACCOUNTS_ORIGIN}/sign-in",
                referer=f"{ACCOUNTS_ORIGIN}/sign-in?redirect=oauth2-provider",
            )
            if not csl.get("ok"):
                raise RuntimeError(f"{label}: CreateCookieSetterLink failed: {csl.get('error')}")
            setter = str(csl.get("cookie_setter_url") or "")
            self._log(f"{label}: cookie_setter={setter[:100]}...")

            # Apply set-cookie hop without overwriting sso incorrectly.
            current = setter
            for _ in range(6):
                if "code=" in current and (
                    current.startswith(redirect_uri) or "127.0.0.1" in current
                ):
                    return self._code_from_url(current, state)
                if "set-cookie" in current:
                    # Only GET set-cookie; use response Set-Cookie (do not clobber sso with config.token).
                    resp = self._get(current, allow_redirects=False)
                    loc = resp.headers.get("location") or resp.headers.get("Location") or ""
                    self._log(f"set-cookie HTTP {resp.status_code} loc={(loc or '')[:120]}")
                    if loc:
                        current = urljoin(current, loc)
                        continue
                    break
                break

            # Consent page (HTML) → server action Allow → code
            if "consent" in current:
                page = self._get(current, allow_redirects=False)
                # If redirected with code already (auto-approve)
                loc = page.headers.get("location") or page.headers.get("Location") or ""
                if loc and "code=" in loc:
                    return self._code_from_url(urljoin(current, loc), state)
                if page.status_code == 200 and "Authorize" in (page.text or ""):
                    return _submit_oauth2_consent(current, page.text or "")
            return self._follow_for_code(current, redirect_uri=redirect_uri, state=state)

        self._log("OAuth PKCE start...")
        try:
            if session_cookies and session_cookies.get("sso"):
                self._set_sso_cookie(session_cookies["sso"])
            code = _complete_via_cookie_setter("session-reuse")
            self._log("authorization code obtained via session cookie-setter")
        except Exception as session_err:
            self._log(f"session-reuse failed ({session_err}); password CreateSession")
            if not email or not password:
                raise RuntimeError(
                    f"OAuth needs password login; prior error: {session_err}"
                ) from session_err
            signin = f"{ACCOUNTS_ORIGIN}/sign-in?redirect=oauth2-provider"
            self._get(signin, allow_redirects=True)
            sess = self.create_session(email, password, referer=signin)
            if not sess.get("ok"):
                raise RuntimeError(
                    f"CreateSession failed: {sess.get('error')}; prior: {session_err}"
                ) from session_err
            # Prefer CreateSession jwt as sso; keep signup sso as fallback.
            jwt = sess.get("session_jwt") or (session_cookies or {}).get("sso")
            if jwt:
                self._set_sso_cookie(str(jwt))
            try:
                code = _complete_via_cookie_setter("password-login")
            except Exception as csl_err:
                self._log(f"cookie-setter path failed ({csl_err}); raw authorize follow")
                code = self._follow_for_code(auth_url, redirect_uri=redirect_uri, state=state)

        self._log("exchanging authorization code...")
        return _finalize_oauth_code(
            code=code,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            client_id=client_id,
            proxy=proxy,
            output_dir=output_dir,
            cliproxyapi_auth_dir=cliproxyapi_auth_dir,
            cliproxyapi_base_url=cliproxyapi_base_url,
            cliproxyapi_disabled=cliproxyapi_disabled,
        )


def login_with_protocol(
    email: str,
    password: str,
    *,
    yescaptcha_key: str = "",
    proxy: str = "",
    debug: bool = False,
    turnstile_premium: bool = True,
    cliproxyapi_auth_dir: Optional[str] = None,
    cliproxyapi_base_url: str = CLIPROXYAPI_GROK_BASE_URL,
    cliproxyapi_disabled: bool = False,
    output_dir: Optional[str] = None,
    redirect_port: int = 56121,
    session_cookies: Optional[Dict[str, str]] = None,
    auth_client: Any = None,
) -> OAuthLoginResult:
    """Convenience wrapper: protocol OAuth + optional Sub2API auth export.

    If *auth_client* (XConsoleAuthClient) is provided after signup, its live
    curl_cffi session is reused so accounts.x.ai cookies stay attached.
    """
    client = ProtocolOAuthClient(
        yescaptcha_key=yescaptcha_key,
        proxy=proxy,
        debug=debug,
        turnstile_premium=turnstile_premium,
    )
    if auth_client is not None:
        try:
            transport = auth_client._t
            session = getattr(transport, "_session", None)
            if session is not None:
                client._s = session
                client._log("reusing XConsoleAuthClient curl_cffi session for OAuth")
        except Exception as exc:
            client._log(f"could not reuse auth client session: {exc}")
            if not session_cookies:
                session_cookies = extract_cookies_from_auth_client(auth_client)
    return client.login(
        email,
        password,
        cliproxyapi_auth_dir=cliproxyapi_auth_dir,
        cliproxyapi_base_url=cliproxyapi_base_url,
        cliproxyapi_disabled=cliproxyapi_disabled,
        output_dir=output_dir,
        redirect_port=redirect_port,
        proxy=proxy,
        session_cookies=session_cookies,
    )
