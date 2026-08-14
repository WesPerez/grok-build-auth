#!/usr/bin/env python3
"""grok-build-auth — 一键注册 x.ai 账号 + SSO + Grok Build OAuth（Sub2API 可用）

流程:
  1) 协议注册（邮箱验证 + Turnstile + create_account）
  2) 提取 SSO
  3) xAI OAuth PKCE（含 grok-cli:access）
  4) 导出 Sub2API auth：cli-chat-proxy.grok.com + grok-cli headers
     → 可直接用 grok-4.6 走 Build/CLI 编码通道

环境变量（按需设置）:
    YESCAPTCHA_API_KEY     YesCaptcha API key (Turnstile 打码)
    TEMPMAIL_API_KEY       Tempmail.lol API key (邮箱后端)
    CLOUDFLARE_API_TOKEN   Cloudflare API token (alias_mail 邮箱后端)
    IMAP_SERVER / IMAP_USERNAME / IMAP_PASSWORD / IMAP_EMAIL  自建 IMAP 邮箱后端
    CLIPROXYAPI_AUTH_DIR   Sub2API auth 目录（历史兼容变量名，可选）
    HTTPS_PROXY / HTTP_PROXY  代理（OAuth 换 token / Playwright 可选）
"""
from __future__ import annotations

import sys
import os
import uuid
import json
import base64
import time
import threading
import argparse
import secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

# Load local .env if present (optional dependency).
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except Exception:
    pass

from xconsole_client import XConsoleAuthClient, config as C
from xconsole_client.xai_oauth import (
    CLIPROXYAPI_GROK_BASE_URL,
    complete_build_oauth,
    default_cliproxyapi_auth_dir,
)
from xconsole_client.oauth_protocol import extract_cookies_from_auth_client
from xconsole_client.registration_backends import (
    BrowserRegistrationRequest,
    CaptchaChallenge,
    YesCaptchaProvider,
    create_registration_backend,
)
from xconsole_client.security import mask_email, redact_text, set_restrictive_umask, validate_cliproxyapi_base_url

# -- secrets from environment only ---------------------------------------
YESCAPTCHA_KEY = os.environ.get("YESCAPTCHA_API_KEY", "")
TEMPMAIL_KEY = os.environ.get("TEMPMAIL_API_KEY", "")
IMAP_SERVER = os.environ.get("IMAP_SERVER", "")
IMAP_USERNAME = os.environ.get("IMAP_USERNAME", "")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")
IMAP_EMAIL = os.environ.get("IMAP_EMAIL", "")
IMAP_PORT = os.environ.get("IMAP_PORT", "")
IMAP_SSL = os.environ.get("IMAP_SSL", "")  # "true" for IMAPS (port 993)
SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"
PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""

_results_lock = threading.Lock()
_cf_lock = threading.Lock()
_oauth_lock = threading.Lock()  # Playwright/browser OAuth is safer serialized
_results: list[dict] = []
_done = 0
_total = 0
_t0 = 0.0


def _write_result_json(path: str | Path, payload: dict) -> None:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = target.with_name(f".{target.name}.{secrets.token_hex(6)}.tmp")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.chmod(0o600)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def _log(i: int, msg: str):
    elapsed = time.time() - _t0
    bar = f"[{_done}/{_total}]" if _total > 1 else ""
    print(f"  {bar} [#{i}] {msg}  ({elapsed:.0f}s)")


def _make_email_provider(backend: str):
    """Return (email, receiver) — receiver has .wait_for_code(timeout)."""
    if backend == "tempmail":
        if not TEMPMAIL_KEY:
            raise RuntimeError("TEMPMAIL_API_KEY 环境变量未设置")
        from xconsole_client.tempmail_transport import TempmailInbox
        inbox = TempmailInbox(api_key=TEMPMAIL_KEY, prefix="xai", debug=False)
        email = inbox.create()
        return email, inbox
    elif backend == "cloudflare":
        from xconsole_client.mailbox import AliasMailAccount, AliasMailCodeReceiver
        with _cf_lock:
            cf = AliasMailAccount.ensure_cf()
            alloc = AliasMailAccount(cf)
            address = alloc.create(prefix="xai")
        receiver = AliasMailCodeReceiver(cf, address=address, timeout=120, interval=3, since_now=True)
        return address, receiver
    elif backend == "imap":
        if not all([IMAP_SERVER, IMAP_USERNAME, IMAP_PASSWORD, IMAP_EMAIL]):
            raise RuntimeError(
                "IMAP 邮箱后端需要设置 IMAP_SERVER, IMAP_USERNAME, IMAP_PASSWORD, IMAP_EMAIL 环境变量"
            )
        from xconsole_client.imap_backend import ImapInbox
        use_ssl = IMAP_SSL.strip().lower() in ("1", "true", "yes", "on")
        port = int(IMAP_PORT) if IMAP_PORT else None
        inbox = ImapInbox(
            server=IMAP_SERVER,
            username=IMAP_USERNAME,
            password=IMAP_PASSWORD,
            email=IMAP_EMAIL,
            use_ssl=use_ssl,
            port=port,
            debug=False,
        )
        email = inbox.create()
        return email, inbox
    else:
        raise ValueError(f"unknown email backend: {backend}")


def register_one(
    index: int,
    email_backend: str = "tempmail",
    *,
    do_oauth: bool = True,
    oauth_headless: bool = True,
    oauth_timeout: float = 180.0,
    oauth_interactive_fallback: bool = False,
    oauth_protocol: bool = True,
    oauth_debug: bool = False,
    cliproxyapi_auth_dir: Optional[str | Path] = None,
    cliproxyapi_base_url: str = CLIPROXYAPI_GROK_BASE_URL,
    proxy_url: str = "",
    registration_backend: str = "protocol-yescaptcha",
    browser_headless: bool = True,
) -> dict:
    """Run signup (+ optional Build OAuth export). Thread-safe."""
    if registration_backend == "protocol-yescaptcha" and not YESCAPTCHA_KEY:
        return {
            "email": "",
            "sso_ok": False,
            "cliproxyapi_auth": None,
            "error": "YESCAPTCHA_API_KEY 环境变量未设置",
        }

    # Per-client signup_url — never mutate global C.SIGNUP_URL under concurrency.
    c = XConsoleAuthClient(debug=False, signup_url=SIGNUP_URL, proxy=proxy_url or None)
    email = ""
    password = ""
    sso = None
    receiver = None
    account_created = False

    try:
        if registration_backend == "browser-playwright-edge":
            email, receiver = _make_email_provider(email_backend)
            password = f"Pw{secrets.token_urlsafe(24)}!a#A"
            _log(index, f"email: {mask_email(email)}")
            browser_proxy_url = os.environ.get("GROK_BROWSER_PROXY_URL", "").strip() or proxy_url
            backend = create_registration_backend(
                registration_backend,
                require_proxy=bool(browser_proxy_url),
            )

            def browser_progress(stage: str, detail: str) -> None:
                _log(index, f"browser-stage={stage}: {detail}")

            browser_result = backend.register(BrowserRegistrationRequest(
                email=email,
                password=password,
                wait_for_code=lambda timeout: receiver.wait_for_code(timeout=timeout),
                signup_url=SIGNUP_URL,
                proxy=browser_proxy_url,
                timeout=max(oauth_timeout, 240.0),
                headless=browser_headless,
                progress=browser_progress,
            ))
            account_created = True
            sso = browser_result.sso
            session_cookies = {
                str(item.get("name")): str(item.get("value"))
                for item in browser_result.cookies
                if item.get("name") and item.get("value")
            }
            _log(index, "account created")
            _log(index, "SSO acquired in browser context")
            result = {
                "email": email,
                "sso_ok": True,
                "cliproxyapi_auth": None,
                "build_base_url": cliproxyapi_base_url,
                "password": password,
                "account_created": True,
                "registration_backend": registration_backend,
                "error": None,
            }
            if do_oauth:
                auth_dir = Path(cliproxyapi_auth_dir) if cliproxyapi_auth_dir else default_cliproxyapi_auth_dir()
                _log(index, f"OAuth Build path → {auth_dir}  (cookies={len(session_cookies)})")
                oauth = complete_build_oauth(
                    email,
                    password,
                    cliproxyapi_auth_dir=auth_dir,
                    cliproxyapi_base_url=cliproxyapi_base_url,
                    headless=oauth_headless,
                    timeout=oauth_timeout,
                    proxy=proxy_url,
                    interactive_fallback=oauth_interactive_fallback,
                    yescaptcha_key=YESCAPTCHA_KEY,
                    protocol=oauth_protocol,
                    playwright_fallback=False,
                    debug=oauth_debug,
                    session_cookies=session_cookies,
                    auth_client=None,
                )
                result["cliproxyapi_auth"] = str(oauth.cliproxyapi_path) if oauth.cliproxyapi_path else None
                if not result["cliproxyapi_auth"] or not Path(result["cliproxyapi_auth"]).is_file():
                    raise RuntimeError("OAuth completed without a Sub2API auth file")
                _log(index, "Build OAuth OK; Sub2API auth written")
            return result

        # 1. warm-up + scrape
        c.visit_home()
        c.load_signup_page()
        _log(index, "cookie + scrape OK")

        # 2. email
        email, receiver = _make_email_provider(email_backend)
        password = f"Pw{secrets.token_urlsafe(24)}!a#A"
        _log(index, f"email: {mask_email(email)}")

        c.require_grpc_success(
            "CreateEmailValidationCode",
            c.create_email_validation_code(email),
        )
        code = receiver.wait_for_code(timeout=120)
        _log(index, "email verification code received")
        c.require_grpc_success(
            "VerifyEmailValidationCode",
            c.verify_email_validation_code(email, code),
        )
        c.validate_password(email, password)
        _log(index, "email verified")

        # 3. turnstile
        captcha = YesCaptchaProvider(YESCAPTCHA_KEY).solve_turnstile(CaptchaChallenge(
            website_url=SIGNUP_URL,
            website_key=C.TURNSTILE_SITEKEY,
            premium=True,
        ))
        turnstile = captcha.token
        _log(index, f"Turnstile {len(turnstile)} chars")

        # 4. create account
        res = c.create_account(
            email=email, given_name="Test", family_name="User",
            password=password, email_validation_code=code,
            turnstile_token=turnstile, castle_request_token="",
            conversion_id=str(uuid.uuid4()),
        )
        if not res.ok:
            reason = getattr(c, "_last_signup_error_reason", "")
            detail = f"RSC error: {redact_text(reason)}" if reason else f"HTTP {res.http_status}"
            _log(index, f"FAIL create_account {detail}")
            return {
                "email": email,
                "sso_ok": False,
                "cliproxyapi_auth": None,
                "password": password,
                "account_created": False,
                "error": f"create_account rejected: {detail}",
            }
        account_created = True
        _log(index, "account created")

        # 5. SSO (retries + RSC chain + grok.com fallback inside client)
        sso = c.fetch_sso_token(email=email, password=password, save=False, retries=3)
        if not sso:
            _log(index, "SSO extraction failed (x.ai may have changed); OAuth will use password login")
        else:
            _log(index, "SSO acquired in memory")

        result = {
            "email": email,
            "sso_ok": bool(sso),
            "cliproxyapi_auth": None,
            "build_base_url": cliproxyapi_base_url,
            "password": password,
            "account_created": True,
            "error": None,
        }

        # 6. OAuth → Sub2API Grok Build path (coding-ready)
        if do_oauth:
            auth_dir = Path(cliproxyapi_auth_dir) if cliproxyapi_auth_dir else default_cliproxyapi_auth_dir()
            # Reuse signup session cookies so OAuth can skip password login when possible.
            session_cookies = extract_cookies_from_auth_client(c)
            _log(index, f"OAuth Build path → {auth_dir}  (cookies={len(session_cookies)})")
            def complete_oauth():
                return complete_build_oauth(
                    email,
                    password,
                    cliproxyapi_auth_dir=auth_dir,
                    cliproxyapi_base_url=cliproxyapi_base_url,
                    headless=oauth_headless,
                    timeout=oauth_timeout,
                    proxy=proxy_url,
                    interactive_fallback=oauth_interactive_fallback,
                    yescaptcha_key=YESCAPTCHA_KEY,
                    protocol=oauth_protocol,
                    playwright_fallback=False,
                    debug=oauth_debug,
                    session_cookies=session_cookies,
                    auth_client=c,
                )

            if oauth_protocol:
                oauth_error = None
                for oauth_attempt in range(1, 4):
                    try:
                        oauth = complete_oauth()
                        break
                    except Exception as exc:
                        oauth_error = exc
                        if oauth_attempt < 3:
                            _log(index, f"OAuth transient failure; retry {oauth_attempt}/2")
                            time.sleep(2 * oauth_attempt)
                else:
                    assert oauth_error is not None
                    raise oauth_error
            else:
                # Callback/browser automation remains serialized; protocol OAuth
                # has no listening callback server and is safe to run concurrently.
                with _oauth_lock:
                    oauth = complete_oauth()
            result["cliproxyapi_auth"] = str(oauth.cliproxyapi_path) if oauth.cliproxyapi_path else None
            if not result["cliproxyapi_auth"] or not Path(result["cliproxyapi_auth"]).is_file():
                raise RuntimeError("OAuth completed without a Sub2API auth file")
            _log(
                index,
                "Build OAuth OK; Sub2API auth written",
            )
        else:
            if not sso:
                raise RuntimeError("SSO extraction failed")
            _log(index, "OAuth skipped (--no-oauth)")

        return result

    except Exception as e:
        _log(index, f"ERROR: {redact_text(e)}")
        return {
            "email": email,
            "sso_ok": bool(sso),
            "cliproxyapi_auth": None,
            "password": password,
            "account_created": account_created,
            "error": redact_text(e),
        }
    finally:
        c.close()
        if receiver is not None:
            try:
                receiver.close() if hasattr(receiver, "close") else None
            except Exception:
                pass
        with _results_lock:
            global _done
            _done += 1


def main() -> int:
    set_restrictive_umask()
    global _total, _t0
    default_auth = str(default_cliproxyapi_auth_dir())
    p = argparse.ArgumentParser(
        description="grok-build-auth: x.ai register + SSO + Grok Build OAuth (Sub2API-ready)",
    )
    p.add_argument("-n", "--count", type=int, choices=[1], default=1, help="账号数量（服务器版仅允许 1）")
    p.add_argument("-t", "--threads", type=int, choices=[1], default=1, help="并发线程数（服务器版仅允许 1）")
    p.add_argument(
        "--proxy-env",
        default="",
        help="从指定环境变量读取本次账号的 sticky proxy URL",
    )
    p.add_argument(
        "--registration-backend",
        choices=["protocol-yescaptcha", "browser-playwright-edge"],
        default="protocol-yescaptcha",
    )
    p.add_argument("--browser-headed", action="store_true")
    p.add_argument(
        "-e", "--email",
        choices=["tempmail", "cloudflare", "imap"],
        default="tempmail",
        help="邮箱后端: tempmail | cloudflare | imap",
    )
    p.add_argument(
        "--result-json",
        default="",
        help="将机器可读结果原子写入指定 JSON（包含账号密码，文件权限 0600）",
    )
    p.add_argument(
        "--no-oauth",
        action="store_true",
        help="只注册+SSO，不走 Build OAuth / Sub2API auth 导出",
    )
    p.add_argument(
        "--cliproxyapi-auth-dir",
        default=default_auth,
        help=f"Sub2API auth 目录（默认: {default_auth}）",
    )
    p.add_argument(
        "--cliproxyapi-base-url",
        default=CLIPROXYAPI_GROK_BASE_URL,
        help="Build 上游 base_url（默认 cli-chat-proxy.grok.com/v1）",
    )
    p.add_argument(
        "--oauth-headed",
        action="store_true",
        help="Playwright 有头模式（仅非协议回退时使用）",
    )
    p.add_argument(
        "--oauth-timeout",
        type=float,
        default=180.0,
        help="OAuth 等待超时秒数",
    )
    p.add_argument(
        "--no-oauth-protocol",
        action="store_true",
        help="禁用纯协议 OAuth（默认用 YesCaptcha+CreateSession，不启浏览器）",
    )
    p.add_argument(
        "--oauth-interactive-fallback",
        action="store_true",
        help="协议/Playwright 失败时回退到系统浏览器手动登录",
    )
    args = p.parse_args()
    args.cliproxyapi_base_url = validate_cliproxyapi_base_url(args.cliproxyapi_base_url)

    _total = args.count
    _t0 = time.time()
    threads = min(args.threads, args.count)
    do_oauth = not args.no_oauth
    proxy_url = os.environ.get(args.proxy_env, "") if args.proxy_env else PROXY

    print(
        f"grok-build-auth: {args.count} accounts, {threads} threads, email={args.email}, "
        f"oauth={'on' if do_oauth else 'off'}"
    )
    if do_oauth:
        print(f"  cliproxyapi-auth-dir: {args.cliproxyapi_auth_dir}")
        print(f"  build-base-url:       {args.cliproxyapi_base_url}")
    print()

    common_kwargs = dict(
        do_oauth=do_oauth,
        oauth_headless=not args.oauth_headed,
        oauth_timeout=args.oauth_timeout,
        oauth_interactive_fallback=args.oauth_interactive_fallback,
        oauth_protocol=not args.no_oauth_protocol,
        oauth_debug=False,
        cliproxyapi_auth_dir=args.cliproxyapi_auth_dir,
        cliproxyapi_base_url=args.cliproxyapi_base_url,
        proxy_url=proxy_url,
        registration_backend=args.registration_backend,
        browser_headless=not args.browser_headed,
    )

    if args.count == 1:
        result = register_one(1, email_backend=args.email, **common_kwargs)
        _results.append(result)
    else:
        with ThreadPoolExecutor(max_workers=threads) as ex:
            futures = [
                ex.submit(register_one, i, args.email, **common_kwargs)
                for i in range(1, args.count + 1)
            ]
            for f in as_completed(futures):
                _results.append(f.result())

    # summary
    ok_build = [r for r in _results if r.get("cliproxyapi_auth") or (r.get("sso_ok") and not do_oauth)]
    ok_sso = [r for r in _results if r.get("sso_ok")]
    fail = [r for r in _results if r.get("error")]
    print(f"\n{'=' * 50}")
    print(
        f"Done in {time.time() - _t0:.0f}s  |  "
        f"SSO OK: {len(ok_sso)}  BUILD OK: {len([r for r in _results if r.get('cliproxyapi_auth')])}  "
        f"FAIL: {len(fail)}"
    )
    print(f"{'=' * 50}")
    for r in _results:
        email = mask_email(r.get("email") or "")
        if r.get("cliproxyapi_auth"):
            print(f"  {email:40s}  BUILD  {r['cliproxyapi_auth']}")
        elif r.get("sso_ok") and not do_oauth:
            print(f"  {email:40s}  SSO OK")
        elif r.get("sso_ok") and r.get("error"):
            print(f"  {email:40s}  SSO-ok OAuth-FAIL: {r.get('error')}")
        else:
            print(f"  {email:40s}  FAIL: {r.get('error', '?')}")

    payload = {
        "ok": not fail,
        "account_count": args.count,
        "success_count": len(ok_build),
        "failure_count": len(fail),
        "results": _results,
    }
    if args.result_json:
        _write_result_json(args.result_json, payload)
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
