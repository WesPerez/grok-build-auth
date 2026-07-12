from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CLIENT = ROOT / "clients" / "windows"
sys.path.insert(0, str(SCRIPTS))

from windows_client_common import (  # noqa: E402
    WindowsClientError,
    require_config,
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
