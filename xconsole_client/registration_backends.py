"""Registration backends and CAPTCHA provider abstractions.

The protocol backend and browser backend intentionally live at different
levels.  A browser registration is not merely a source of a Turnstile token:
the token, browser fingerprint, cookies, and network exit belong to one page
session, so the browser backend completes the whole signup flow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Dict, Iterable, Optional, Protocol
from urllib.parse import unquote, urlparse

from . import config as C
from .solver import YesCaptchaSolver


ProgressCallback = Callable[[str, str], None]
CodeLoader = Callable[[float], str]


@dataclass(frozen=True)
class CaptchaChallenge:
    website_url: str
    website_key: str
    timeout: float = 120.0
    premium: bool = True


@dataclass(frozen=True)
class CaptchaSolution:
    token: str
    provider: str
    elapsed_seconds: float
    network_mode: str


class CaptchaProvider(Protocol):
    """Provider used by HTTP/protocol registration backends."""

    name: str
    network_mode: str
    experimental: bool

    def solve_turnstile(self, challenge: CaptchaChallenge) -> CaptchaSolution:
        ...


class YesCaptchaProvider:
    """Adapter preserving the existing YesCaptcha protocol behavior."""

    name = "yescaptcha"
    network_mode = "proxyless"
    experimental = False

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = "https://api.yescaptcha.com",
        poll_interval: float = 3.0,
        debug: bool = False,
        solver_factory: Callable[..., YesCaptchaSolver] = YesCaptchaSolver,
    ) -> None:
        if not (api_key or "").strip():
            raise ValueError("YesCaptcha API key is required")
        self._api_key = api_key.strip()
        self._endpoint = endpoint
        self._poll_interval = poll_interval
        self._debug = debug
        self._solver_factory = solver_factory

    def solve_turnstile(self, challenge: CaptchaChallenge) -> CaptchaSolution:
        started = time.monotonic()
        solver = self._solver_factory(
            self._api_key,
            endpoint=self._endpoint,
            timeout=challenge.timeout,
            poll_interval=self._poll_interval,
            debug=self._debug,
        )
        token = solver.solve_turnstile(
            challenge.website_url,
            challenge.website_key,
            premium=challenge.premium,
        )
        return CaptchaSolution(
            token=token,
            provider=self.name,
            elapsed_seconds=round(time.monotonic() - started, 3),
            network_mode=self.network_mode,
        )


@dataclass(frozen=True)
class BrowserRegistrationRequest:
    email: str
    password: str
    wait_for_code: CodeLoader
    given_name: str = "Test"
    family_name: str = "User"
    signup_url: str = "https://accounts.x.ai/sign-up?redirect=grok-com"
    proxy: str = ""
    timeout: float = 240.0
    code_timeout: float = 120.0
    headless: bool = False
    progress: Optional[ProgressCallback] = None


@dataclass(frozen=True)
class BrowserRegistrationResult:
    email: str
    password: str
    sso: str
    cookies: list[dict[str, Any]] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    backend: str = "browser-playwright-edge"
    experimental: bool = True


class RegistrationBackend(Protocol):
    name: str
    experimental: bool

    def register(self, request: BrowserRegistrationRequest) -> BrowserRegistrationResult:
        ...


def _edge_executable(candidates: Optional[Iterable[str]] = None) -> str:
    paths = tuple(candidates or (
        "/usr/bin/microsoft-edge",
        "/usr/bin/microsoft-edge-stable",
        "/usr/bin/microsoft-edge-dev",
        "/opt/microsoft/msedge/msedge",
        "/opt/microsoft/msedge-dev/microsoft-edge-dev",
    ))
    for value in paths:
        path = Path(value)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise RuntimeError("Microsoft Edge executable was not found")


def _playwright_proxy(proxy_url: str) -> Optional[Dict[str, str]]:
    value = (proxy_url or "").strip()
    if not value:
        return None
    parsed = urlparse(value if "://" in value else f"http://{value}")
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
        raise ValueError("browser proxy must be an http, https, socks5, or socks5h URL")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("browser proxy URL must not contain a path, query, or fragment")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    port = f":{parsed.port}" if parsed.port else ""
    # Playwright accepts socks5 but not requests-style socks5h. Chromium still
    # sends hostname-based CONNECT requests through the SOCKS server.
    playwright_scheme = "socks5" if parsed.scheme == "socks5h" else parsed.scheme
    result = {
        "server": f"{playwright_scheme}://{host}{port}",
        # OAuth callback and local health endpoints must never use the external proxy.
        "bypass": "127.0.0.1,localhost,::1",
    }
    if parsed.username is not None:
        result["username"] = unquote(parsed.username)
        result["password"] = unquote(parsed.password or "")
    return result


class PlaywrightBrowserRegistrationBackend:
    """Experimental full-page signup using a fresh Edge browser context.

    No direct-network retry is performed when a proxy is configured.  Any
    launch or navigation failure aborts the attempt, preserving the account's
    network identity.
    """

    name = "browser-playwright-edge"
    experimental = True

    def __init__(
        self,
        *,
        executable_path: str = "",
        require_proxy: bool = False,
        playwright_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.executable_path = executable_path or _edge_executable()
        self.require_proxy = require_proxy
        self._playwright_factory = playwright_factory

    @staticmethod
    def _emit(request: BrowserRegistrationRequest, stage: str, detail: str) -> None:
        if request.progress is not None:
            request.progress(stage, detail)

    @staticmethod
    def _visible_locator(page: Any, selectors: Iterable[str], timeout_ms: int = 15000) -> Any:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            for selector in selectors:
                locator = page.locator(selector)
                try:
                    if locator.count() and locator.first.is_visible():
                        return locator.first
                except Exception:
                    continue
            page.wait_for_timeout(200)
        controls = []
        try:
            controls = page.locator("input,button").evaluate_all("""
                els => els.slice(0, 20).map(el => ({
                  tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || '',
                  name: el.getAttribute('name') || '', autocomplete: el.getAttribute('autocomplete') || '',
                  placeholder: el.getAttribute('placeholder') || '', text: (el.innerText || '').trim().slice(0, 60)
                }))
            """)
        except Exception:
            pass
        raise TimeoutError(f"required signup control did not become visible; controls={controls}")

    @classmethod
    def _click_continue(cls, page: Any) -> None:
        button = cls._visible_locator(page, (
            'button[type="submit"]',
            'button:has-text("Continue")',
            'button:has-text("Next")',
            'button:has-text("继续")',
            'button:has-text("下一步")',
        ))
        button.click()

    @classmethod
    def _fill_email(cls, page: Any, email: str) -> None:
        if not page.locator('input[type="email"], input[name="email"], input[autocomplete="email"]').count():
            email_signup = cls._visible_locator(page, (
                'button:has-text("Sign up with email")',
                'a:has-text("Sign up with email")',
                'button:has-text("使用电子邮件注册")',
            ))
            email_signup.click()
        control = cls._visible_locator(page, (
            'input[type="email"]', 'input[name="email"]', 'input[autocomplete="email"]',
        ))
        control.fill(email)
        cls._click_continue(page)

    @staticmethod
    def _raise_if_blocked(page: Any) -> None:
        title = str(page.title() or "") if hasattr(page, "title") else ""
        body = str(page.locator("body").inner_text(timeout=3000) or "") if hasattr(page, "locator") else ""
        lowered = f"{title}\n{body}".lower()
        if "sorry, you have been blocked" in lowered or "blocked due to abusive traffic patterns" in lowered:
            ray = re.search(r"cloudflare ray id:\s*([a-z0-9]+)", body, re.IGNORECASE)
            suffix = f" (ray {ray.group(1)})" if ray else ""
            raise RuntimeError(f"x.ai blocked the browser egress before signup{suffix}")

    @classmethod
    def _fill_code(cls, page: Any, code: str) -> None:
        one = None
        try:
            one = cls._visible_locator(page, (
                'input[autocomplete="one-time-code"]',
                'input[name*="code" i]',
            ), timeout_ms=5000)
        except TimeoutError:
            pass
        if one is not None:
            one.fill(code)
        else:
            boxes = page.locator('input[inputmode="numeric"], input[maxlength="1"]')
            if boxes.count() < len(code):
                raise RuntimeError("verification-code controls were not found")
            for index, char in enumerate(code):
                boxes.nth(index).fill(char)
        cls._click_continue(page)
        try:
            page.locator('input[autocomplete="one-time-code"], input[name*="code" i]').first.wait_for(
                state="hidden", timeout=20000,
            )
        except Exception as exc:
            raise TimeoutError("email confirmation did not advance to the profile step") from exc

    @classmethod
    def _fill_profile(cls, page: Any, request: BrowserRegistrationRequest) -> None:
        given = cls._visible_locator(page, (
            'input[data-testid="givenName"]', 'input[name="givenName"]',
            'input[autocomplete="given-name"]',
        ))
        family = cls._visible_locator(page, (
            'input[data-testid="familyName"]', 'input[name="familyName"]',
            'input[autocomplete="family-name"]',
        ))
        password = cls._visible_locator(page, (
            'input[data-testid="password"]', 'input[name="password"]',
            'input[type="password"]', 'input[autocomplete="new-password"]',
        ))
        given.fill(request.given_name)
        family.fill(request.family_name)
        password.fill(request.password)

    @staticmethod
    def _turnstile_token(page: Any) -> str:
        try:
            return str(page.evaluate("""
                () => {
                  const input = document.querySelector('input[name="cf-turnstile-response"]');
                  if (input && input.value) return String(input.value).trim();
                  if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
                    return String(window.turnstile.getResponse() || '').trim();
                  }
                  return '';
                }
            """) or "").strip()
        except Exception:
            return ""

    @classmethod
    def _wait_for_turnstile(cls, page: Any, deadline: float) -> str:
        last_click = 0.0
        while time.monotonic() < deadline:
            token = cls._turnstile_token(page)
            if len(token) >= 80:
                return token
            now = time.monotonic()
            if now - last_click >= 3:
                for frame in getattr(page, "frames", []):
                    if "challenges.cloudflare.com" not in str(getattr(frame, "url", "")):
                        continue
                    try:
                        checkbox = frame.locator('input[type="checkbox"], [role="checkbox"]').first
                        if checkbox.is_visible():
                            checkbox.click(timeout=1500)
                            break
                    except Exception:
                        continue
                last_click = now
            page.wait_for_timeout(500)
        raise TimeoutError("browser Turnstile did not produce a token before timeout")

    @staticmethod
    def _sso_cookie(cookies: Iterable[dict[str, Any]]) -> str:
        for cookie in cookies:
            if cookie.get("name") == "sso" and cookie.get("value"):
                return str(cookie["value"])
        return ""

    def register(self, request: BrowserRegistrationRequest) -> BrowserRegistrationResult:
        if not request.email or not request.password:
            raise ValueError("email and password are required")
        proxy = _playwright_proxy(request.proxy)
        if self.require_proxy and proxy is None:
            raise RuntimeError("browser backend requires a proxy for this run")
        if not request.headless and not os.environ.get("DISPLAY"):
            raise RuntimeError("headed browser registration requires DISPLAY or a dedicated Xvfb")

        factory = self._playwright_factory
        if factory is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise RuntimeError("playwright is required for browser registration") from exc
            factory = sync_playwright

        started = time.monotonic()
        deadline = started + max(30.0, request.timeout)
        launch: dict[str, Any] = {
            "headless": request.headless,
            "executable_path": self.executable_path,
        }
        if proxy is not None:
            launch["proxy"] = proxy

        self._emit(request, "browser-launch", "starting isolated Edge context")
        with factory() as playwright:
            # Playwright raises on an unreachable configured proxy.  There is
            # deliberately no catch-and-relaunch path without that proxy.
            browser = playwright.chromium.launch(**launch)
            try:
                context = browser.new_context(
                    user_agent=C.USER_AGENT,
                    viewport={"width": 1280, "height": 900},
                    locale="en-US",
                )
                try:
                    page = context.new_page()
                    self._emit(request, "signup", "opening x.ai signup page")
                    page.goto(
                        request.signup_url,
                        wait_until="domcontentloaded",
                        timeout=int(min(request.timeout, 60.0) * 1000),
                    )
                    self._raise_if_blocked(page)
                    self._fill_email(page, request.email)

                    self._emit(request, "email-verification", "waiting for mailbox code")
                    code = str(request.wait_for_code(request.code_timeout) or "").strip()
                    if not code:
                        raise RuntimeError("mailbox returned an empty verification code")
                    self._fill_code(page, code)

                    self._emit(request, "profile", "filling account profile")
                    self._fill_profile(page, request)
                    self._emit(request, "turnstile", "waiting for browser Turnstile")
                    self._wait_for_turnstile(page, deadline)
                    self._click_continue(page)

                    self._emit(request, "sso", "waiting for authenticated session cookie")
                    sso = ""
                    cookies: list[dict[str, Any]] = []
                    while time.monotonic() < deadline:
                        cookies = list(context.cookies())
                        sso = self._sso_cookie(cookies)
                        if sso:
                            break
                        page.wait_for_timeout(500)
                    if not sso:
                        raise TimeoutError("browser signup completed without an sso cookie")
                    return BrowserRegistrationResult(
                        email=request.email,
                        password=request.password,
                        sso=sso,
                        # Keep only cookies needed by the downstream OAuth path.
                        cookies=[item for item in cookies if item.get("name") == "sso"],
                        elapsed_seconds=round(time.monotonic() - started, 3),
                    )
                finally:
                    context.close()
            finally:
                browser.close()


def create_captcha_provider(name: str, **kwargs: Any) -> CaptchaProvider:
    normalized = (name or "yescaptcha").strip().lower()
    if normalized in {"yescaptcha", "protocol", "protocol-yescaptcha"}:
        return YesCaptchaProvider(**kwargs)
    raise ValueError(f"unknown CAPTCHA provider: {name}")


def create_registration_backend(name: str, **kwargs: Any) -> RegistrationBackend:
    normalized = (name or "browser").strip().lower()
    if normalized in {"browser", "browser-playwright", "browser-playwright-edge"}:
        return PlaywrightBrowserRegistrationBackend(**kwargs)
    raise ValueError(f"unknown registration backend: {name}")
