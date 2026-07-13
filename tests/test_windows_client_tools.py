from __future__ import annotations

import importlib.util
import json
import sys
import urllib.request
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CLIENT = ROOT / "clients" / "windows"
sys.path.insert(0, str(SCRIPTS))

from windows_client_common import (  # noqa: E402
    WindowsClientError,
    require_config,
    responses_probe,
    validate_bridge_result,
)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_require_config_rejects_placeholders():
    with pytest.raises(WindowsClientError, match="remote"):
        require_config({"remote": "<secret>"}, "remote")


def test_windows_preflight_supports_configured_bridge_health_path():
    preflight = load_module("windows_client_preflight_test", SCRIPTS / "windows_client_preflight.py")
    assert preflight.bridge_health_url({
        "cloudflare_api_base": "https://bridge.example/",
        "bridge_health_path": "/bridge-health",
    }) == "https://bridge.example/bridge-health"
    with pytest.raises(WindowsClientError, match="absolute path"):
        preflight.bridge_health_url({
            "cloudflare_api_base": "https://bridge.example",
            "bridge_health_path": "https://evil.example/health",
        })


def test_validate_bridge_result_requires_probe_and_created(tmp_path):
    auth = tmp_path / "xai-test.json"
    auth.write_text("{}", encoding="utf-8")
    result = {
        "ok": True,
        "path": str(auth),
        "pushed": True,
        "push_response": {"probe": "passed", "action": "created", "account_id": 1},
    }
    summary = validate_bridge_result(result, require_created=True)
    assert summary["push_response"]["account_id"] == 1

    result["push_response"]["action"] = "updated"
    with pytest.raises(WindowsClientError, match="not a new account"):
        validate_bridge_result(result, require_created=True)


def test_responses_probe_ignores_environment_proxy(monkeypatch):
    handlers = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            return json.dumps({
                "status": "completed",
                "output": [{"content": [{"text": "WINDOWS_CLIENT_OK"}]}],
            }).encode()

    class Opener:
        def open(self, request, timeout):
            return Response()

    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:10900")

    def build_opener(*items):
        handlers.extend(items)
        return Opener()

    monkeypatch.setattr(
        "windows_client_common.urllib.request.build_opener",
        build_opener,
    )
    monkeypatch.setattr(
        "windows_client_common.urllib.request.urlopen",
        lambda *args, **kwargs: pytest.fail("responses_probe used the environment-aware global opener"),
    )
    assert responses_probe("https://sub2api.example", "secret")["output_ok"] is True
    assert any(
        isinstance(handler, urllib.request.ProxyHandler) and handler.proxies == {}
        for handler in handlers
    )


def test_cpa_export_requires_bridge_probe(tmp_path, monkeypatch):
    sys.path.insert(0, str(CLIENT))
    try:
        cpa_export = load_module("windows_cpa_export_test", CLIENT / "cpa_export.py")
        import cpa
        import oidc_mint

        monkeypatch.setattr(
            oidc_mint,
            "mint_with_browser",
            lambda **kwargs: {
                "access_token": "not-a-jwt-but-nonempty",
                "refresh_token": "refresh-token-value",
                "expires_in": 21600,
            },
        )
        monkeypatch.setattr(oidc_mint, "resolve_proxy", lambda value: value)
        monkeypatch.setattr(oidc_mint, "set_runtime_proxy", lambda value: None)
        monkeypatch.setattr(
            cpa,
            "push_auth_file",
            lambda **kwargs: (True, 200, json.dumps({"status": "ok", "probe": "passed", "action": "created"})),
        )
        result = cpa_export.export_cpa_for_account(
            "test@example.com",
            "password",
            page=object(),
            config={
                "cpa_auth_dir": str(tmp_path),
                "cpa_preprobe_enabled": False,
                "cpa_push_enabled": True,
                "cpa_push_required": True,
                "cpa_require_probe_passed": True,
                "cpa_remote_base": "https://bridge.example",
                "cpa_remote_secret": "secret",
            },
            log_callback=lambda message: None,
        )
        assert result["ok"] is True
        assert result["pushed"] is True
        assert result["push_response"]["probe"] == "passed"

        monkeypatch.setattr(
            cpa,
            "push_auth_file",
            lambda **kwargs: (True, 200, json.dumps({"status": "ok"})),
        )
        result = cpa_export.export_cpa_for_account(
            "test2@example.com",
            "password",
            page=object(),
            config={
                "cpa_auth_dir": str(tmp_path),
                "cpa_preprobe_enabled": False,
                "cpa_push_enabled": True,
                "cpa_push_required": True,
                "cpa_require_probe_passed": True,
                "cpa_remote_base": "https://bridge.example",
                "cpa_remote_secret": "secret",
            },
            log_callback=lambda message: None,
        )
        assert result["ok"] is False
        assert result["pushed"] is False
    finally:
        sys.path.remove(str(CLIENT))


def test_windows_main_has_no_global_process_kill():
    source = (CLIENT / "grok_register_ttk.py").read_text(encoding="utf-8-sig")
    cleanup = source.split("def cleanup_stray_chrome", 1)[1].split("def create_browser_options", 1)[0]
    assert "process_iter" not in cleanup
    assert "googleupdate.exe" not in cleanup.lower()
    assert "export_result = export_cpa_after_register" in source
    assert 'response.get("probe") != "passed"' in source

def test_client_preprobe_decisions(monkeypatch):
    sys.path.insert(0, str(CLIENT))
    try:
        from cpa import preprobe

        class FakeResp:
            def __init__(self, status_code, payload=None, text=""):
                self.status_code = status_code
                self._payload = payload
                self.text = text if text or payload is None else json.dumps(payload)

            def json(self):
                if self._payload is None:
                    raise ValueError("no json")
                return self._payload

        class FakeSession:
            def __init__(self):
                self.trust_env = True
                self._response = None

            def post(self, *args, **kwargs):
                return self._response

        auth = {"access_token": "tok", "refresh_token": "ref"}

        sess = FakeSession()
        monkeypatch.setattr(preprobe.requests, "Session", lambda: sess)
        monkeypatch.setattr(preprobe.secrets, "token_hex", lambda n: "fixed")

        sess._response = FakeResp(200, {"status": "completed", "output_text": "CLIENT_PROBE_OK_fixed"})
        assert preprobe.probe_auth(auth)["decision"] == "pass"

        sess._response = FakeResp(200, {
            "status": "completed", "input": "Reply exactly: CLIENT_PROBE_OK_fixed", "output": []
        })
        assert preprobe.probe_auth(auth)["code"] == "INCOMPLETE_RESPONSE"

        sess._response = FakeResp(200, {"status": "in_progress", "output_text": "nope"})
        assert preprobe.probe_auth(auth)["code"] == "INCOMPLETE_RESPONSE"

        sess._response = FakeResp(403, text='{"error":"permission-denied"}')
        got = preprobe.probe_auth(auth)
        assert got["decision"] == "retry"
        assert got["code"] == "PERMISSION_DENIED"

        sess._response = FakeResp(429, text="free-usage exhausted")
        assert preprobe.probe_auth(auth)["decision"] == "cooldown"

        sess._response = FakeResp(401, text="invalid_grant refresh revoked")
        assert preprobe.probe_auth(auth)["decision"] == "refresh"

        sess._response = FakeResp(401, text="invalid_client")
        assert preprobe._refresh_with_requests(auth, timeout=1)["code"] == "REFRESH_FAILED"

        assert preprobe.probe_auth({"access_token": "x"})["decision"] == "reject"
    finally:
        if str(CLIENT) in sys.path:
            sys.path.remove(str(CLIENT))


def test_cpa_export_required_preprobe_cannot_be_disabled(tmp_path, monkeypatch):
    sys.path.insert(0, str(CLIENT))
    try:
        cpa_export = load_module("windows_cpa_export_required_test", CLIENT / "cpa_export.py")
        import oidc_mint
        monkeypatch.setattr(oidc_mint, "mint_with_browser", lambda **kwargs: {
            "access_token": "token", "refresh_token": "refresh"
        })
        monkeypatch.setattr(oidc_mint, "resolve_proxy", lambda value: value)
        monkeypatch.setattr(oidc_mint, "set_runtime_proxy", lambda value: None)
        with pytest.raises(ValueError, match="forbids disabling"):
            cpa_export.export_cpa_for_account(
                "required@example.com", "password", page=object(),
                config={"cpa_auth_dir": str(tmp_path / "cpa_auths"),
                        "cpa_preprobe_required": True, "cpa_preprobe_enabled": False},
                log_callback=lambda message: None,
            )
    finally:
        if str(CLIENT) in sys.path:
            sys.path.remove(str(CLIENT))


def test_cpa_export_preprobe_pending_not_push(tmp_path, monkeypatch):
    sys.path.insert(0, str(CLIENT))
    try:
        cpa_export = load_module("windows_cpa_export_preprobe_test", CLIENT / "cpa_export.py")
        import cpa
        import oidc_mint

        monkeypatch.setattr(
            oidc_mint,
            "mint_with_browser",
            lambda **kwargs: {
                "access_token": "not-a-jwt-but-nonempty",
                "refresh_token": "refresh-token-value",
                "expires_in": 21600,
            },
        )
        monkeypatch.setattr(oidc_mint, "resolve_proxy", lambda value: value)
        monkeypatch.setattr(oidc_mint, "set_runtime_proxy", lambda value: None)
        monkeypatch.setattr(
            cpa.preprobe,
            "probe_auth",
            lambda auth, proxy="", timeout=45: {
                "decision": "retry",
                "code": "PERMISSION_DENIED",
                "status": 403,
            },
        )
        pushed = {"n": 0}

        def boom_push(**kwargs):
            pushed["n"] += 1
            return True, 200, json.dumps({"probe": "passed", "action": "created"})

        monkeypatch.setattr(cpa, "push_auth_file", boom_push)
        out = tmp_path / "cpa_auths"
        result = cpa_export.export_cpa_for_account(
            "pending@example.com",
            "password",
            page=object(),
            config={
                "cpa_auth_dir": str(out),
                "cpa_preprobe_enabled": True,
                "cpa_preprobe_required": True,
                "cpa_push_enabled": True,
                "cpa_push_required": True,
                "cpa_require_probe_passed": True,
                "cpa_remote_base": "https://bridge.example",
                "cpa_remote_secret": "secret",
            },
            log_callback=lambda message: None,
        )
        assert result["ok"] is False
        assert result["preprobe"]["code"] == "PERMISSION_DENIED"
        assert result.get("side_dir") == "cpa_pending"
        assert pushed["n"] == 0
        assert not list(out.glob("xai-*.json"))
        pending = out.parent / "cpa_pending"
        assert list(pending.glob("xai-*.json"))
    finally:
        if str(CLIENT) in sys.path:
            sys.path.remove(str(CLIENT))
