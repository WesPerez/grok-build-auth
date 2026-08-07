from __future__ import annotations

import importlib.util
import stat
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "migrate_legacy_bridge.py"


def load_module():
    spec = importlib.util.spec_from_file_location("bridge_migration_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_literal_assignments_only_reads_named_constants(tmp_path):
    source = tmp_path / "bridge.py"
    source.write_text(
        "\n".join([
            'MAILU_API_TOKEN = "mailu-secret"',
            'SUB2API_ADMIN_KEY = "sub2api-secret"',
            'MAILU_API_BASE = "https://mail.example/api"',
            'MAILU_DOMAIN = "example.com"',
            'MAILU_IMAP_HOST = "mail.example.com"',
            "MAILU_IMAP_PORT = 993",
            'SUB2API_BASE = "http://127.0.0.1:13080"',
            'SUB2API_POSTGRES_CONTAINER = "postgres"',
            'SUB2API_PG_USER = "user"',
            'SUB2API_PG_DB = "db"',
            "BRIDGE_PORT = 8190",
            'UNRELATED = "ignored"',
        ]),
        encoding="utf-8",
    )
    values = load_module().literal_assignments(source)
    assert values["MAILU_API_TOKEN"] == "mailu-secret"
    assert "UNRELATED" not in values


def test_atomic_secret_is_private(tmp_path):
    path = tmp_path / "secret"
    load_module().atomic_secret(path, "value")
    assert path.read_text(encoding="utf-8") == "value\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
