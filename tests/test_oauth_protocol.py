from __future__ import annotations

import json
from urllib.parse import unquote

from xconsole_client.oauth_protocol import (
    ProtocolOAuthClient,
    SESSION_COOKIE_DOMAINS,
    build_consent_action_request,
    classify_consent_action_error,
    consent_principal_is_valid,
    extract_consent_action_error,
    extract_consent_form_values,
    extract_consent_router_state_tree,
    extract_next_deployment_id,
)


class _FakeCookies:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def set(self, name: str, value: str, *, domain: str | None = None) -> None:
        self.calls.append((name, value, domain))


class _FakeSession:
    def __init__(self) -> None:
        self.cookies = _FakeCookies()


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200, headers: dict | None = None) -> None:
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}


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


def test_extract_consent_form_values_preserves_server_identity() -> None:
    html = """
      <form>
        <input value="User" name="principal_type" type="hidden">
        <input type="hidden" name="principal_id" value="user-123">
        <input name="scope" value="openid offline_access">
        <input name="referrer" value="grok-cli&amp;desktop">
        <input name="unrelated" value="ignored">
      </form>
    """

    assert extract_consent_form_values(html) == {
        "principal_type": "User",
        "principal_id": "user-123",
        "scope": "openid offline_access",
        "referrer": "grok-cli&desktop",
    }


def test_extract_consent_form_values_does_not_guess_missing_identity() -> None:
    assert extract_consent_form_values('<input name="scope" value="openid">') == {
        "scope": "openid",
    }


def test_extract_consent_form_values_decodes_next_flight_props() -> None:
    flight = {
        "principalType": "User",
        "principalId": "user-from-flight",
        "scope": "openid offline_access",
        "referrer": "grok-cli",
    }
    payload = json.dumps([1, json.dumps(flight, separators=(",", ":"))])
    html = f"<script>self.__next_f.push({payload})</script>"

    assert extract_consent_form_values(html) == {
        "principal_type": "User",
        "principal_id": "user-from-flight",
        "scope": "openid offline_access",
        "referrer": "grok-cli",
    }


def test_extract_consent_form_values_decodes_nested_flight_json() -> None:
    inner = json.dumps({"principalType": "User", "principalId": "nested-user"})
    nested = json.dumps({"consent": inner}, separators=(",", ":"))
    payload = json.dumps([1, nested])

    assert extract_consent_form_values(
        f"<script>self.__next_f.push({payload})</script>"
    ) == {
        "principal_type": "User",
        "principal_id": "nested-user",
    }


def test_flight_identity_overrides_empty_ssr_placeholders() -> None:
    payload = json.dumps([
        1,
        json.dumps({"principalType": "User", "principalId": "hydrated-user"}),
    ])
    html = (
        '<input name="principal_type" value="">'
        '<input name="principal_id" value="">'
        f'<script>self.__next_f.push({payload})</script>'
    )

    values = extract_consent_form_values(html)
    assert values["principal_type"] == "User"
    assert values["principal_id"] == "hydrated-user"


def test_user_principal_may_have_empty_id_in_current_consent_contract() -> None:
    values = extract_consent_form_values(
        '<input name="principal_type" value="User">'
        '<input name="principal_id" value="">'
    )

    assert values["principal_type"] == "User"
    assert values["principal_id"] == ""
    assert consent_principal_is_valid("User", "") is True
    assert consent_principal_is_valid("Team", "") is False
    assert consent_principal_is_valid("Team", "team-123") is True


def test_extract_consent_action_error_decodes_flight_escaping() -> None:
    response = '1:{\\"success\\":false,\\"error\\":\\"Invalid OAuth request\\"}'
    assert extract_consent_action_error(response) == "Invalid OAuth request"


def test_access_denied_is_classified_as_authorization_rejection() -> None:
    assert classify_consent_action_error("Access denied") == "authorization"


def test_extract_consent_router_state_uses_current_flight_tree() -> None:
    raw_tree = [
        "",
        {"children": [
            "(app)",
            {"children": [
                "(auth)",
                {"children": [
                    "oauth2",
                    {"children": [
                        "consent",
                        {"children": ["__PAGE__?{\"state\":\"secret\"}", {}]},
                    ]},
                ]},
            ]},
        ]},
        "$undefined",
        "$undefined",
        16,
    ]
    flight = {"f": [[raw_tree, None, None, False]]}
    payload = json.dumps([1, "0:" + json.dumps(flight, separators=(",", ":"))])
    html = f"<script>self.__next_f.push({payload})</script>"

    prepared = json.loads(unquote(extract_consent_router_state_tree(html)))

    assert prepared[0] == ""
    assert prepared[2:] == [None, None, 16]
    page = (
        prepared[1]["children"][1]["children"][1]["children"][1]
        ["children"][1]["children"]
    )
    assert page[0] == "__PAGE__"
    assert "$undefined" not in json.dumps(prepared)


def test_build_consent_action_request_preserves_canonical_query_once() -> None:
    page_url = (
        "https://accounts.x.ai/oauth2/consent?client_id=cli&state=state-1"
        "&code_challenge=challenge#ignored"
    )
    target, headers, body = build_consent_action_request(
        page_url=page_url,
        action_id="40" + "a" * 40,
        router_state_tree="%5B%22%22%2C%7B%7D%5D",
        action_args=[{"action": "allow", "state": "state-1"}],
        deployment_id="deployment-1",
    )

    assert target == page_url.split("#", 1)[0]
    assert "client_id=cli" in target and "state=state-1" in target
    assert headers["referer"] == target
    assert headers["next-action"] == "40" + "a" * 40
    assert headers["x-deployment-id"] == "deployment-1"
    assert "next-url" not in headers
    assert json.loads(body) == [{"action": "allow", "state": "state-1"}]


def test_extract_next_deployment_id_is_header_safe() -> None:
    assert extract_next_deployment_id('<html data-dpl-id="deployment-1">') == "deployment-1"
    assert extract_next_deployment_id('<html data-dpl-id="bad&#10;header">') == ""


def test_consent_intermediate_redirect_continues_oauth_chain() -> None:
    client = _client()
    calls: list[tuple[str, str, str]] = []
    client._follow_for_code = lambda url, *, redirect_uri, state: (
        calls.append((url, redirect_uri, state)) or "oauth-code"
    )

    code = client._code_from_consent_redirect(
        _FakeResponse("", status_code=303, headers={"location": "/oauth2/continue"}),
        page_url="https://accounts.x.ai/oauth2/consent?state=state-1",
        redirect_uri="http://127.0.0.1:56121/callback",
        state="state-1",
    )

    assert code == "oauth-code"
    assert calls == [(
        "https://accounts.x.ai/oauth2/continue",
        "http://127.0.0.1:56121/callback",
        "state-1",
    )]


def test_next_action_redirect_header_continues_oauth_chain() -> None:
    client = _client()
    calls: list[tuple[str, str, str]] = []
    client._follow_for_code = lambda url, *, redirect_uri, state: (
        calls.append((url, redirect_uri, state)) or "oauth-code"
    )

    code = client._code_from_consent_redirect(
        _FakeResponse(
            "",
            headers={"x-action-redirect": "/oauth2/continue;push"},
        ),
        page_url="https://accounts.x.ai/oauth2/consent?state=state-1",
        redirect_uri="http://127.0.0.1:56121/callback",
        state="state-1",
    )

    assert code == "oauth-code"
    assert calls == [(
        "https://accounts.x.ai/oauth2/continue",
        "http://127.0.0.1:56121/callback",
        "state-1",
    )]


def test_consent_callback_redirect_extracts_code_without_http_follow() -> None:
    client = _client()
    client._follow_for_code = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("callback redirect must not be fetched")
    )

    code = client._code_from_consent_redirect(
        _FakeResponse(
            "",
            status_code=303,
            headers={
                "Location": "http://127.0.0.1:56121/callback?code=oauth-code&state=state-1"
            },
        ),
        page_url="https://accounts.x.ai/oauth2/consent",
        redirect_uri="http://127.0.0.1:56121/callback",
        state="state-1",
    )

    assert code == "oauth-code"
