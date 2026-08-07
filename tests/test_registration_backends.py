from __future__ import annotations

from unittest import mock

import pytest

from xconsole_client.registration_backends import (
    BrowserRegistrationRequest,
    CaptchaChallenge,
    PlaywrightBrowserRegistrationBackend,
    YesCaptchaProvider,
    _playwright_proxy,
    create_captcha_provider,
    create_registration_backend,
)


def test_yescaptcha_provider_preserves_proxyless_premium_behavior():
    solver = mock.Mock()
    solver.solve_turnstile.return_value = "token"
    factory = mock.Mock(return_value=solver)
    provider = YesCaptchaProvider(
        "api-key",
        endpoint="https://captcha.example",
        poll_interval=1.5,
        solver_factory=factory,
    )

    solution = provider.solve_turnstile(CaptchaChallenge(
        website_url="https://accounts.x.ai/sign-up",
        website_key="site-key",
        timeout=45,
        premium=True,
    ))

    factory.assert_called_once_with(
        "api-key",
        endpoint="https://captcha.example",
        timeout=45,
        poll_interval=1.5,
        debug=False,
    )
    solver.solve_turnstile.assert_called_once_with(
        "https://accounts.x.ai/sign-up", "site-key", premium=True,
    )
    assert solution.token == "token"
    assert solution.provider == "yescaptcha"
    assert solution.network_mode == "proxyless"


def test_yescaptcha_control_plane_does_not_inherit_registration_proxy():
    from xconsole_client.solver import YesCaptchaSolver

    solver = YesCaptchaSolver("api-key")
    assert solver._session.trust_env is False


def test_provider_factories_are_explicit():
    assert isinstance(create_captcha_provider("protocol", api_key="key"), YesCaptchaProvider)
    assert isinstance(
        create_registration_backend("browser", executable_path="/bin/true"),
        PlaywrightBrowserRegistrationBackend,
    )
    with pytest.raises(ValueError, match="unknown CAPTCHA provider"):
        create_captcha_provider("browser-patch")


def test_playwright_proxy_keeps_auth_out_of_server_and_bypasses_loopback():
    result = _playwright_proxy("http://user%40pool:p%40ss@proxy.example:8080")
    assert result == {
        "server": "http://proxy.example:8080",
        "username": "user@pool",
        "password": "p@ss",
        "bypass": "127.0.0.1,localhost,::1",
    }


def test_playwright_proxy_normalizes_requests_socks5h_scheme():
    assert _playwright_proxy("socks5h://proxy.example:1080")["server"] == "socks5://proxy.example:1080"


@pytest.mark.parametrize("value", [
    "ftp://proxy.example:21",
    "http://proxy.example:8080/path",
    "http://proxy.example:8080?mode=direct",
])
def test_playwright_proxy_rejects_unsupported_or_ambiguous_urls(value):
    with pytest.raises(ValueError, match="browser proxy"):
        _playwright_proxy(value)


class _FakePage:
    def __init__(self):
        self.goto_calls = []

    def goto(self, *args, **kwargs):
        self.goto_calls.append((args, kwargs))

    def wait_for_timeout(self, _milliseconds):
        return None


class _FakeContext:
    def __init__(self):
        self.page = _FakePage()
        self.closed = False

    def new_page(self):
        return self.page

    def cookies(self):
        return [
            {"name": "tracking", "value": "discard"},
            {"name": "sso", "value": "header.payload.signature", "domain": "accounts.x.ai"},
        ]

    def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self):
        self.context = _FakeContext()
        self.context_kwargs = None
        self.closed = False

    def new_context(self, **kwargs):
        self.context_kwargs = kwargs
        return self.context

    def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, browser=None, error=None):
        self.browser = browser or _FakeBrowser()
        self.error = error
        self.launch_calls = []

    def launch(self, **kwargs):
        self.launch_calls.append(kwargs)
        if self.error:
            raise self.error
        return self.browser


class _FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium


class _PlaywrightContextManager:
    def __init__(self, chromium):
        self.playwright = _FakePlaywright(chromium)

    def __enter__(self):
        return self.playwright

    def __exit__(self, *_args):
        return False


class _TestBrowserBackend(PlaywrightBrowserRegistrationBackend):
    events = []

    @classmethod
    def _fill_email(cls, page, email):
        cls.events.append(("email", email))

    @classmethod
    def _fill_code(cls, page, code):
        cls.events.append(("code", code))

    @classmethod
    def _fill_profile(cls, page, request):
        cls.events.append(("profile", request.given_name, request.family_name))

    @classmethod
    def _wait_for_turnstile(cls, page, deadline):
        cls.events.append(("turnstile", deadline > 0))
        return "t" * 100

    @classmethod
    def _click_continue(cls, page):
        cls.events.append(("submit",))


def test_browser_backend_uses_one_isolated_context_and_closes_it(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    browser = _FakeBrowser()
    chromium = _FakeChromium(browser=browser)
    progress = []
    _TestBrowserBackend.events = []
    backend = _TestBrowserBackend(
        executable_path="/bin/true",
        require_proxy=True,
        playwright_factory=lambda: _PlaywrightContextManager(chromium),
    )

    result = backend.register(BrowserRegistrationRequest(
        email="owner@example.com",
        password="secret-password",
        wait_for_code=lambda timeout: "123456",
        proxy="http://user:pass@proxy.example:8080",
        headless=True,
        progress=lambda stage, detail: progress.append((stage, detail)),
    ))

    assert len(chromium.launch_calls) == 1
    assert chromium.launch_calls[0]["proxy"]["server"] == "http://proxy.example:8080"
    assert chromium.launch_calls[0]["proxy"]["username"] == "user"
    assert browser.context.closed
    assert browser.closed
    assert result.sso == "header.payload.signature"
    assert [item["name"] for item in result.cookies] == ["sso"]
    assert _TestBrowserBackend.events == [
        ("email", "owner@example.com"),
        ("code", "123456"),
        ("profile", "Test", "User"),
        ("turnstile", True),
        ("submit",),
    ]
    assert [stage for stage, _ in progress] == [
        "browser-launch", "signup", "email-verification", "profile", "turnstile", "sso",
    ]


def test_browser_backend_never_relaunches_without_failed_proxy(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    chromium = _FakeChromium(error=RuntimeError("proxy connection failed"))
    backend = PlaywrightBrowserRegistrationBackend(
        executable_path="/bin/true",
        playwright_factory=lambda: _PlaywrightContextManager(chromium),
    )
    request = BrowserRegistrationRequest(
        email="owner@example.com",
        password="secret-password",
        wait_for_code=lambda timeout: "123456",
        proxy="http://proxy.example:8080",
        headless=True,
    )

    with pytest.raises(RuntimeError, match="proxy connection failed"):
        backend.register(request)
    assert len(chromium.launch_calls) == 1
    assert "proxy" in chromium.launch_calls[0]


def test_browser_backend_requires_display_for_headed_mode(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    backend = PlaywrightBrowserRegistrationBackend(executable_path="/bin/true")
    with pytest.raises(RuntimeError, match="dedicated Xvfb"):
        backend.register(BrowserRegistrationRequest(
            email="owner@example.com",
            password="secret-password",
            wait_for_code=lambda timeout: "123456",
            headless=False,
        ))


def test_browser_backend_can_require_sticky_proxy(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":99")
    backend = PlaywrightBrowserRegistrationBackend(
        executable_path="/bin/true", require_proxy=True,
    )
    with pytest.raises(RuntimeError, match="requires a proxy"):
        backend.register(BrowserRegistrationRequest(
            email="owner@example.com",
            password="secret-password",
            wait_for_code=lambda timeout: "123456",
            headless=False,
        ))
