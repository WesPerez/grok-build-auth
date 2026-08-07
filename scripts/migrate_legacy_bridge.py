#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import os
import secrets
from pathlib import Path


SECRET_NAMES = {"MAILU_API_TOKEN", "SUB2API_ADMIN_KEY"}
CONFIG_NAMES = {
    "MAILU_API_BASE", "MAILU_DOMAIN", "MAILU_IMAP_HOST", "MAILU_IMAP_PORT",
    "SUB2API_BASE", "SUB2API_POSTGRES_CONTAINER", "SUB2API_PG_USER", "SUB2API_PG_DB",
    "BRIDGE_PORT",
}


def literal_assignments(path: Path) -> dict[str, object]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id not in SECRET_NAMES | CONFIG_NAMES:
            continue
        try:
            values[target.id] = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            continue
    return values


def atomic_secret(path: Path, value: str) -> None:
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value.strip() + "\n")
        os.replace(temp, path)
        path.chmod(0o600)
    finally:
        temp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate the legacy Grok bridge into private credential files")
    parser.add_argument("--legacy-dir", required=True)
    parser.add_argument("--private-dir", required=True)
    parser.add_argument("--group-id", type=int, required=True)
    args = parser.parse_args()

    legacy = Path(args.legacy_dir).resolve()
    private = Path(args.private_dir).resolve()
    source = legacy / "bridge.py"
    values = literal_assignments(source)
    missing = sorted((SECRET_NAMES | CONFIG_NAMES) - values.keys())
    if missing:
        raise SystemExit("legacy bridge is missing literal values: " + ", ".join(missing))
    if args.group_id < 1:
        raise SystemExit("group id must be positive")

    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    private.chmod(0o700)
    secrets_dir = private / "bridge-secrets"
    secrets_dir.mkdir(mode=0o700, exist_ok=True)
    secrets_dir.chmod(0o700)

    mailu_token = secrets_dir / "mailu-api-token"
    sub2api_key = secrets_dir / "sub2api-admin-key"
    management_key = secrets_dir / "management-key"
    jwt_secret = secrets_dir / "jwt-secret"
    atomic_secret(mailu_token, str(values["MAILU_API_TOKEN"]))
    atomic_secret(sub2api_key, str(values["SUB2API_ADMIN_KEY"]))
    atomic_secret(management_key, (legacy / "mgmt_key.txt").read_text(encoding="utf-8"))
    atomic_secret(jwt_secret, (legacy / "bridge_secret.txt").read_text(encoding="utf-8"))

    lines = [
        f"MAILU_API_BASE={values['MAILU_API_BASE']}",
        f"MAILU_API_TOKEN_FILE={mailu_token}",
        f"MAILU_DOMAIN={values['MAILU_DOMAIN']}",
        f"MAILU_IMAP_HOST={values['MAILU_IMAP_HOST']}",
        f"MAILU_IMAP_PORT={values['MAILU_IMAP_PORT']}",
        "",
        f"SUB2API_BASE={values['SUB2API_BASE']}",
        f"SUB2API_ADMIN_KEY_FILE={sub2api_key}",
        f"SUB2API_GROK_GROUP_ID={args.group_id}",
        f"SUB2API_POSTGRES_CONTAINER={values['SUB2API_POSTGRES_CONTAINER']}",
        f"SUB2API_PG_USER={values['SUB2API_PG_USER']}",
        f"SUB2API_PG_DB={values['SUB2API_PG_DB']}",
        "",
        f"BRIDGE_MANAGEMENT_KEY_FILE={management_key}",
        f"BRIDGE_JWT_SECRET_FILE={jwt_secret}",
        f"BRIDGE_PORT={values['BRIDGE_PORT']}",
    ]
    atomic_secret(private / "bridge.env", "\n".join(lines))
    print(f"migrated bridge credentials to {private}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
