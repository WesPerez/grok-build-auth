from __future__ import annotations

from unittest import mock

import pytest

from xconsole_client.client import XConsoleAuthClient
from xconsole_client.models import GrpcResult


def bare_client() -> XConsoleAuthClient:
    client = object.__new__(XConsoleAuthClient)
    client.signup_url = "https://accounts.x.ai/sign-up?redirect=grok-com"
    client._next_action_id = "action"
    client._next_router_state_tree = "state"
    client._last_rsc_body = ""
    client._last_create_set_cookies = []
    client._base_headers = lambda: {}
    return client


def test_require_grpc_success_reports_transport_and_grpc_status():
    result = GrpcResult(
        ok=False,
        http_status=200,
        grpc_status=7,
        trailers={"grpc-message": "permission denied"},
    )
    with pytest.raises(RuntimeError, match=r"gRPC 7: permission denied"):
        XConsoleAuthClient.require_grpc_success("VerifyEmailValidationCode", result)


def test_validate_password_rejects_failed_grpc_result():
    client = bare_client()
    client._grpc_call = mock.Mock(return_value=GrpcResult(False, 503, None))
    with pytest.raises(RuntimeError, match=r"ValidatePassword failed \(HTTP 503"):
        client.validate_password("owner@example.com", "password")


@pytest.mark.parametrize(
    ("body", "cookies", "expected"),
    [
        ('1:{"error":"email already exists"}', [], False),
        ('1:{"success":false}', ["last-logged-in-with=password; Path=/"], False),
        ('1:{"component":"Error: boundary placeholder"}', [], True),
        ('1:{"error":"$undefined"}', [], True),
        ('1:{"error":"null"}', [], True),
        ('1:{"error":"$28"}', [], True),
        ("", [], True),
        ("0:opaque-response", ["last-logged-in-with=password; Path=/"], True),
        (
            '1:"https://auth.grokusercontent.com/set-cookie?q=eyJabc.def.ghi"',
            [],
            True,
        ),
    ],
)
def test_create_account_rejects_only_explicit_business_errors(body, cookies, expected):
    client = bare_client()
    client._request = mock.Mock(return_value=(200, {}, cookies, body.encode()))
    result = client.create_account(
        email="owner@example.com",
        given_name="Test",
        family_name="User",
        password="password",
        email_validation_code="ABC123",
        turnstile_token="turnstile",
        castle_request_token="",
        conversion_id="conversion",
    )
    assert result.ok is expected


def test_create_account_rejects_http_failure_even_with_cookie():
    client = bare_client()
    client._request = mock.Mock(
        return_value=(409, {}, ["last-logged-in-with=password; Path=/"], b"")
    )
    result = client.create_account(
        email="owner@example.com",
        given_name="Test",
        family_name="User",
        password="password",
        email_validation_code="ABC123",
        turnstile_token="turnstile",
        castle_request_token="",
        conversion_id="conversion",
    )
    assert not result.ok
