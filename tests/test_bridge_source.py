from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridge" / "bridge.py"


def load_bridge(monkeypatch, tmp_path):
    for name in ("mailu", "sub2api", "management", "jwt"):
        path = tmp_path / name
        path.write_text(f"{name}-secret", encoding="utf-8")
        path.chmod(0o600)
        monkeypatch.setenv(f"{name.upper()}_TOKEN_FILE", str(path))
    monkeypatch.setenv("MAILU_API_TOKEN_FILE", str(tmp_path / "mailu"))
    monkeypatch.setenv("MAILU_API_BASE", "https://mail.example/api")
    monkeypatch.setenv("MAILU_DOMAIN", "example.com")
    monkeypatch.setenv("MAILU_IMAP_HOST", "mail.example.com")
    monkeypatch.setenv("SUB2API_ADMIN_KEY_FILE", str(tmp_path / "sub2api"))
    monkeypatch.setenv("SUB2API_GROK_GROUP_ID", "5")
    monkeypatch.setenv("SUB2API_POSTGRES_CONTAINER", "postgres")
    monkeypatch.setenv("SUB2API_PG_USER", "sub2api")
    monkeypatch.setenv("SUB2API_PG_DB", "sub2api")
    monkeypatch.setenv("BRIDGE_MANAGEMENT_KEY_FILE", str(tmp_path / "management"))
    monkeypatch.setenv("BRIDGE_JWT_SECRET_FILE", str(tmp_path / "jwt"))
    spec = importlib.util.spec_from_file_location("bridge_source_test", BRIDGE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bridge_uses_secret_files_and_signed_mailbox_tokens(monkeypatch, tmp_path):
    bridge = load_bridge(monkeypatch, tmp_path)
    token = bridge.sign_jwt({"email": "test@example.com", "password": "hidden", "exp": int(time.time()) + 60})
    payload = bridge.verify_jwt(token)
    assert payload["email"] == "test@example.com"
    assert bridge.SUB2API_GROK_GROUP_ID == 5


def test_bridge_probe_parses_json(monkeypatch, tmp_path):
    bridge = load_bridge(monkeypatch, tmp_path)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            return json.dumps({"data": {"success": True}}).encode()

    monkeypatch.setattr(bridge.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    assert bridge.test_sub2api_account(1) is True


def test_bridge_source_contains_no_embedded_production_secret():
    source = BRIDGE.read_text(encoding="utf-8")
    assert "mailu_api_" not in source
    assert "sub2api_admin_" not in source
