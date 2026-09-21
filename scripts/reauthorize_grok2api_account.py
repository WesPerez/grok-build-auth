#!/usr/bin/env python3
"""Export a fresh OAuth grant for one existing, revoked Grok2API identity.

Defaults to a read-only plan. Never registers an account, refreshes old exports,
imports a batch, or changes the existing registration manifests.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys
from urllib.parse import quote

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


class RecoveryError(RuntimeError):
    """A bounded, credential-free failure code."""


def load_target(database: Path, account_id: int) -> dict:
    uri = "file:" + quote(str(database.resolve()), safe="/") + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=3) as db:
        db.execute("PRAGMA query_only=ON")
        db.row_factory = sqlite3.Row
        row = db.execute("""SELECT a.id,a.provider,a.email,a.user_id,a.team_id,
            a.identity_key,a.auth_status,a.enabled,c.refresh_permanent,c.last_refresh_error
            FROM provider_accounts a JOIN account_credentials c ON c.account_id=a.id
            WHERE a.id=?""", (account_id,)).fetchone()
    if row is None or row["provider"] != "grok_build":
        raise RecoveryError("target_not_grok_build")
    return dict(row)


def require_revoked(target: dict) -> None:
    if (target["auth_status"] != "reauthRequired" or not target["refresh_permanent"]
            or target["last_refresh_error"] != "invalid_grant"):
        raise RecoveryError("target_not_revoked")
    if not target["email"] or not target["user_id"]:
        raise RecoveryError("target_identity_incomplete")
    identity = f"grok_build|user|{target['user_id']}|{target['team_id'] or ''}"
    if hashlib.sha256(identity.encode()).hexdigest() != target["identity_key"]:
        raise RecoveryError("stored_identity_mismatch")


def find_material(private: Path, target: dict) -> dict:
    email = target["email"].strip().lower()
    matches = []
    for manifest_path in sorted((private / "runs").glob("*/manifest.json")):
        manifest = json.loads(manifest_path.read_text())
        for index, attempt in enumerate(manifest.get("attempts") or []):
            if str(attempt.get("email") or "").strip().lower() == email:
                matches.append((manifest_path.parent, index, attempt))
    if len(matches) != 1:
        raise RecoveryError("material_missing" if not matches else "material_ambiguous")
    batch, index, attempt = matches[0]
    results_root = (batch / "results").resolve()
    path = Path(str(attempt.get("result_file") or results_root / (email.split("@", 1)[0] + ".json"))).resolve()
    if not path.is_relative_to(results_root):
        raise RecoveryError("result_outside_batch")
    if not path.is_file():
        raise RecoveryError("result_missing")
    rows = [row for row in json.loads(path.read_text()).get("results", [])
            if str(row.get("email") or "").strip().lower() == email]
    if len(rows) != 1:
        raise RecoveryError("result_identity_ambiguous")
    password = str(rows[0].get("password") or "")
    proxy_ref = str(attempt.get("proxy_ref") or "")
    if not password:
        raise RecoveryError("password_missing")
    if not proxy_ref or proxy_ref == "direct":
        raise RecoveryError("resin_identity_missing")
    return {"email": email, "password": password, "proxy_ref": proxy_ref,
            "batch": batch.name, "attempt_index": index}


def load_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        parts = shlex.split(value)
        values[key.strip()] = parts[0] if parts else ""
    return values


def validate_auth(auth: dict, target: dict) -> None:
    if auth.get("type") != "xai" or auth.get("auth_kind") != "oauth":
        raise RecoveryError("unexpected_auth_type")
    if str(auth.get("email") or "").strip().lower() != target["email"].strip().lower():
        raise RecoveryError("new_email_mismatch")
    if auth.get("sub") != target["user_id"]:
        raise RecoveryError("new_subject_mismatch")
    # Match Grok2API's import rule: explicit team_id, otherwise the ID-token
    # claims (or access-token claims when no ID token was issued).
    encoded = str(auth.get("id_token") or auth.get("access_token") or "")
    claims = {}
    if encoded.count(".") == 2:
        try:
            payload = encoded.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if not isinstance(claims, dict):
                raise ValueError("claims must be an object")
        except (ValueError, TypeError):
            raise RecoveryError("invalid_token_claims") from None
    team_id = str(auth.get("team_id") or claims.get("team_id") or "")
    if team_id != str(target["team_id"] or ""):
        raise RecoveryError("new_team_mismatch")
    if not auth.get("access_token") or not auth.get("refresh_token"):
        raise RecoveryError("new_tokens_incomplete")


def write_private_json(path: Path, data: dict) -> None:
    with path.open("x") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(data, stream, indent=2)
        stream.write("\n")


def execute(args, target: dict, material: dict, proxy: str, captcha_key: str) -> dict:
    require_revoked(target)
    if args.expected_identity != target["identity_key"]:
        raise RecoveryError("plan_identity_changed")
    if not args.backup:
        raise RecoveryError("matching_backup_required")
    if args.backup.resolve() == args.database.resolve():
        raise RecoveryError("backup_is_live_database")
    if load_target(args.backup, args.account_id)["identity_key"] != target["identity_key"]:
        raise RecoveryError("backup_identity_mismatch")
    if not args.output_dir:
        raise RecoveryError("output_directory_required")
    output = args.output_dir.resolve()
    if not output.is_relative_to(args.private_dir.resolve() / "runs"):
        raise RecoveryError("output_outside_private_runs")
    if output.exists():
        raise RecoveryError("output_already_exists")
    output.mkdir(mode=0o700, parents=True)
    write_private_json(output / "plan.json", {"account_id": args.account_id,
        "identity_sha256": target["identity_key"], "status": "reauthorizing"})
    from xconsole_client.oauth_protocol import login_with_protocol
    # Both native tokens and compatible exports must stay in this private run.
    # No browser fallback and no outer retries: an uncertain request is reviewed.
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            oauth = login_with_protocol(material["email"], material["password"],
                proxy=proxy, yescaptcha_key=captcha_key, debug=False,
                output_dir=str(output / "native"), cliproxyapi_auth_dir=str(output / "auth"))
    except Exception as exc:
        summary = {"account_id": args.account_id, "status": "reauthorization_failed",
                   "error_type": type(exc).__name__}
        write_private_json(output / "result.json", summary)
        return summary
    for path in (oauth.path, oauth.cliproxyapi_path):
        if path is None or not Path(path).resolve().is_relative_to(output) or not Path(path).is_file():
            raise RecoveryError("oauth_output_outside_run")
    auth_path = Path(oauth.cliproxyapi_path)
    validate_auth(json.loads(auth_path.read_text()), target)
    current = load_target(args.database, args.account_id)
    require_revoked(current)
    if current["identity_key"] != target["identity_key"]:
        raise RecoveryError("target_changed_during_authorization")
    summary = {"account_id": args.account_id, "status": "exported_identity_verified",
               "identity_sha256": target["identity_key"],
               "auth_sha256": hashlib.sha256(auth_path.read_bytes()).hexdigest()}
    write_private_json(output / "result.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--database", type=Path, default=Path("/var/lib/docker/volumes/grok2api_grok2api-data/_data/backend.db"))
    parser.add_argument("--private-dir", type=Path, default=Path("/root/grok-build-auth/private"))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-identity")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.account_id <= 0:
        parser.error("account ID must be positive")
    if args.execute and not (args.expected_identity and args.backup and args.output_dir):
        parser.error("--execute requires --expected-identity, --backup, and --output-dir")
    try:
        target = load_target(args.database, args.account_id)
        require_revoked(target)
        material = find_material(args.private_dir, target)
        config = load_env(args.private_dir / "runtime.env")
        from xconsole_client.proxy_pool import load_proxy_pool
        pool = load_proxy_pool(config.get("GROK_PROXY_POOL_FILE", ""), config)
        if pool.schema_version != 2 or any(spec.source != "resin" for spec in pool.specs):
            raise RecoveryError("resin_v2_pool_required")
        proxy = pool.url_for(material["proxy_ref"])
        if not config.get("YESCAPTCHA_API_KEY"):
            raise RecoveryError("captcha_key_missing")
        summary = {"account_id": args.account_id, "status": "plan_ready",
            "identity_sha256": target["identity_key"], "unique_material": True,
            "resin_proxy_resolved": bool(proxy), "import_performed": False}
        if args.execute:
            with (args.private_dir / "grok2api-reauth.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Recheck after acquiring the operation lock.
                target = load_target(args.database, args.account_id)
                summary = execute(args, target, material, proxy, config["YESCAPTCHA_API_KEY"])
        print(json.dumps(summary))
        return 0 if summary["status"] in ("plan_ready", "exported_identity_verified") else 1
    except RecoveryError as exc:
        print(json.dumps({"account_id": args.account_id, "status": "blocked", "reason": str(exc)}))
        return 1
    except Exception as exc:
        # Exceptions from dependencies can contain credentials; never print them.
        print(json.dumps({"account_id": args.account_id, "status": "failed", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
