from __future__ import annotations

from xconsole_client.oauth_protocol import ProtocolOAuthClient, SESSION_COOKIE_DOMAINS


class _FakeCookies:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def set(self, name: str, value: str, *, domain: str | None = None) -> None:
        self.calls.append((name, value, domain))


class _FakeSession:
    def __init__(self) -> None:
        self.cookies = _FakeCookies()


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


def _client() -> ProtocolOAuthClient:
    client = object.__new__(ProtocolOAuthClient)
    client.debug = False
    client._s = _FakeSession()
    return client


def test_load_cookies_clones_only_allowlisted_session_cookies() -> None:
    client = _client()

    client.load_cookies({
        "sso": "session-token",
        "sso-rw": "rw-token",
        "cf_clearance": "clearance-token",
        "unrelated": "ignored",
    })

    calls = client._s.cookies.calls
    assert {name for name, _, _ in calls} == {"sso", "sso-rw", "cf_clearance"}
    assert {domain for _, _, domain in calls} == set(SESSION_COOKIE_DOMAINS)
    assert all(name != "unrelated" for name, _, _ in calls)


def test_set_sso_cookie_populates_read_and_write_cookie_names_on_auth_hosts() -> None:
    client = _client()

    client._set_sso_cookie("session-token")

    assert set(client._s.cookies.calls) == {
        (name, "session-token", domain)
        for name in ("sso", "sso-rw")
        for domain in SESSION_COOKIE_DOMAINS
    }


def test_find_consent_action_id_uses_named_server_reference() -> None:
    action_id = "7f" + "a" * 40
    source = f'createServerReference)("{action_id}",callServer)/* submitOAuth2Consent */'

    assert ProtocolOAuthClient._find_consent_action_id(source) == action_id


def test_resolve_consent_action_id_searches_live_script_chunks() -> None:
    client = _client()
    action_id = "7f" + "b" * 40
    html = '<script src="/_next/static/chunks/app/consent.js"></script>'
    client._get = lambda url, headers=None: _FakeResponse(
        f'const submitOAuth2Consent=createServerReference)("{action_id}",callServer)'
    )

    assert client._resolve_consent_action_id("https://accounts.x.ai/oauth2/consent", html) == action_id
