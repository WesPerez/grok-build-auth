#!/usr/bin/env python3
"""Batch x.ai registration and one-shot Sub2API import.

This orchestrator keeps each registration isolated, aggregates successful auth
files into one bundle, and calls the Sub2API import helper exactly once. The
helper therefore creates one database backup per batch, regardless of count.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse
import urllib.error
import urllib.request


PROJECT_DIR = Path(__file__).resolve().parents[1]
EMAIL_RE = re.compile(r"^(xai[a-f0-9]{6})@([A-Za-z0-9.-]+)$")


class BatchError(RuntimeError):
    pass


def load_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise BatchError(f"private runtime config not found: {path}")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.chmod(0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    log: Path | None = None,
    input_text: str | None = None,
    on_line: Any = None,
) -> subprocess.CompletedProcess[str]:
    if on_line is None:
        proc = subprocess.run(
            command,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            input=input_text,
            check=False,
        )
    else:
        child = subprocess.Popen(
            command,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if input_text is not None else None,
            bufsize=1,
        )
        if input_text is not None and child.stdin is not None:
            child.stdin.write(input_text)
            child.stdin.close()
        output: list[str] = []
        assert child.stdout is not None
        for line in child.stdout:
            output.append(line)
            on_line(line.rstrip("\n"))
        child.wait()
        proc = subprocess.CompletedProcess(command, child.returncode, "".join(output), None)
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        log.write_text(proc.stdout, encoding="utf-8")
        log.chmod(0o600)
    return proc


def helper_env(config: dict[str, str]) -> dict[str, str]:
    """Return a minimal environment for the loopback Sub2API helper."""
    env = os.environ.copy()
    parsed = urlparse(config["SUB2API_URL"])
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise BatchError("Sub2API URL must be loopback for this server workflow")
    for key in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        env.pop(key, None)
    env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    env["no_proxy"] = env["NO_PROXY"]
    return env


def latest_resumable_batch(private_dir: Path) -> Path:
    runs = private_dir / "runs"
    candidates: list[Path] = []
    if runs.is_dir():
        for path in runs.iterdir():
            manifest_path = path / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                status = json.loads(manifest_path.read_text(encoding="utf-8")).get("status")
            except (OSError, json.JSONDecodeError):
                continue
            if status in {"import-failed", "import-verification-failed", "registered-not-imported"}:
                candidates.append(path)
    if not candidates:
        raise BatchError("no resumable batch found")
    return sorted(candidates, key=lambda item: item.name)[-1]


def resolve_resume_batch(private_dir: Path, value: str) -> Path:
    batch_dir = latest_resumable_batch(private_dir) if value == "latest" else private_dir / "runs" / value
    batch_dir = batch_dir.resolve()
    runs_root = (private_dir / "runs").resolve()
    if batch_dir.parent != runs_root or not (batch_dir / "manifest.json").is_file():
        raise BatchError(f"invalid batch to resume: {value}")
    return batch_dir


def load_resume_bundle(batch_dir: Path) -> tuple[dict[str, Any], Path, list[Path]]:
    manifest_path = batch_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") == "completed":
        raise BatchError("batch is already completed")
    bundle_path = Path(str(manifest.get("bundle") or "")).resolve()
    expected = (batch_dir / "bundle" / "sub2api-bundle.json").resolve()
    if bundle_path != expected or not bundle_path.is_file():
        raise BatchError("resume bundle is missing or outside the batch directory")
    digest = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    if digest != manifest.get("bundle_sha256"):
        raise BatchError("resume bundle hash does not match manifest")
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    auth_paths = [Path(str(item["auth_file"])).resolve() for item in manifest.get("attempts", []) if item.get("status") == "registered"]
    if len(bundle.get("accounts", [])) != len(auth_paths) or not auth_paths:
        raise BatchError("resume account count does not match registered attempts")
    return manifest, bundle_path, auth_paths


def require_config(config: dict[str, str], keys: list[str]) -> None:
    missing = [key for key in keys if not config.get(key)]
    if missing:
        raise BatchError("missing runtime config keys: " + ", ".join(missing))


def validate_grok_target_config(config: dict[str, str]) -> str:
    if config.get("SUB2API_GROUP") != "grok":
        raise BatchError("SUB2API_GROUP must be grok for Grok OAuth accounts")
    value = config.get("GROK_ACCOUNT_BASE_URL", "").rstrip("/")
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "grok-cli-proxy"
        or parsed.port != 8080
        or parsed.path != "/v1"
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise BatchError("GROK_ACCOUNT_BASE_URL must be http://grok-cli-proxy:8080/v1")
    return value


def mailbox_exists(db_path: Path, email: str) -> bool:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        row = conn.execute("select count(*) from user where email = ?", (email,)).fetchone()
    return bool(row and row[0] == 1)


def validate_batch_email(email: str, domain: str) -> tuple[str, str]:
    match = EMAIL_RE.fullmatch(email)
    if not match or match.group(2).lower() != domain.lower():
        raise BatchError(f"refusing non-batch mailbox target: {email}")
    return match.group(1), match.group(2)


def create_mailbox(config: dict[str, str], email: str) -> None:
    localpart, domain = validate_batch_email(email, config["MAILU_DOMAIN"])
    if mailbox_exists(Path(config["MAILU_DB"]), email):
        raise BatchError(f"refusing to reuse existing Mailu user: {email}")
    command = [
        "docker", "exec", "-i", config["MAILU_ADMIN_CONTAINER"],
        "sh", "-lc",
        'IFS= read -r password; exec "$1" mailu user "$2" "$3" "$password"',
        "mailu-batch", config["MAILU_FLASK_BIN"], localpart, domain,
    ]
    proc = run(command, input_text=config["IMAP_PASSWORD"] + "\n")
    if proc.returncode != 0:
        if mailbox_exists(Path(config["MAILU_DB"]), email):
            delete_mailbox_exact(config, email)
        raise BatchError(f"Mailu user creation failed for {email}: exit {proc.returncode}")
    if not mailbox_exists(Path(config["MAILU_DB"]), email):
        raise BatchError(f"Mailu CLI returned without creating {email}")


def delete_mailbox_exact(config: dict[str, str], email: str) -> None:
    validate_batch_email(email, config["MAILU_DOMAIN"])
    proc = run([
        "docker", "exec", config["MAILU_ADMIN_CONTAINER"],
        config["MAILU_FLASK_BIN"], "mailu", "user-delete", "--really", email,
    ])
    if proc.returncode != 0 and mailbox_exists(Path(config["MAILU_DB"]), email):
        raise BatchError(f"Mailu exact rollback failed for {email}: exit {proc.returncode}")
    if mailbox_exists(Path(config["MAILU_DB"]), email):
        raise BatchError(f"Mailu exact rollback left user present: {email}")
    maildir = f"{config['MAILU_MAIL_ROOT'].rstrip('/')}/{email}"
    proc = run([
        "docker", "exec", config["MAILU_IMAP_CONTAINER"],
        "rm", "-rf", "--", maildir,
    ])
    if proc.returncode != 0:
        raise BatchError(f"Mailu exact Maildir rollback failed for {email}: exit {proc.returncode}")
    check = run(["docker", "exec", config["MAILU_IMAP_CONTAINER"], "test", "!", "-e", maildir])
    if check.returncode != 0:
        raise BatchError(f"Mailu exact Maildir rollback left path present: {email}")


def load_single_result(path: Path, expected_email: str, auth_dir: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != 1:
        raise BatchError("run.py result JSON must contain exactly one result")
    result = results[0]
    if result.get("email") != expected_email:
        raise BatchError("run.py result email does not match the current mailbox")
    if result.get("error"):
        raise BatchError(str(result["error"]))
    raw_auth = result.get("cliproxyapi_auth")
    if not raw_auth:
        raise BatchError("run.py did not return an auth path")
    auth_path = Path(raw_auth).resolve()
    auth_root = auth_dir.resolve()
    if auth_path.parent != auth_root or not auth_path.is_file():
        raise BatchError("auth path is missing or outside the current attempt directory")
    auth = json.loads(auth_path.read_text(encoding="utf-8"))
    if auth.get("email") != expected_email:
        raise BatchError("auth JSON email does not match the current mailbox")
    if not auth.get("access_token"):
        raise BatchError("auth JSON has no access token")
    result["cliproxyapi_auth"] = str(auth_path)
    return result


def upstream_account_created(path: Path) -> bool | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        results = payload.get("results")
        if not isinstance(results, list) or len(results) != 1:
            return None
        value = results[0].get("account_created")
        return value if isinstance(value, bool) else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def account_from_auth(auth_path: Path, account_base_url: str) -> dict[str, Any]:
    auth = json.loads(auth_path.read_text(encoding="utf-8"))
    token = str(auth.get("access_token") or "")
    if not token:
        raise BatchError(f"auth file has no access token: {auth_path}")
    return {
        "name": auth.get("email", ""),
        "platform": "grok",
        "type": "oauth",
        "credentials": {
            "access_token": token,
            "refresh_token": auth.get("refresh_token", ""),
            "id_token": auth.get("id_token", ""),
            "token_type": auth.get("token_type", "Bearer"),
            "email": auth.get("email", ""),
            "sub": auth.get("sub", ""),
            "expires_at": auth.get("expires_at"),
            "base_url": account_base_url,
        },
        "extra": {
            "base_url": account_base_url,
            "oauth_source_base_url": auth.get("base_url", "https://cli-chat-proxy.grok.com/v1"),
            "redirect_uri": auth.get("redirect_uri", ""),
            "token_endpoint": auth.get("token_endpoint", ""),
            "access_token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        },
        "concurrency": 1,
        "priority": 5,
        "auto_pause_on_expired": True,
    }


def build_bundle(auth_paths: list[Path], account_base_url: str) -> dict[str, Any]:
    accounts = [account_from_auth(path, account_base_url) for path in auth_paths]
    emails = [str(account.get("name") or "").lower() for account in accounts]
    hashes = [str((account.get("extra") or {}).get("access_token_sha256") or "") for account in accounts]
    if len(set(emails)) != len(emails) or len(set(hashes)) != len(hashes):
        raise BatchError("duplicate email or access-token hash in batch auth files")
    return {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "proxies": [],
        "accounts": accounts,
    }


def auth_token_hash(auth_path: Path) -> str:
    auth = json.loads(auth_path.read_text(encoding="utf-8"))
    token = str(auth.get("access_token") or "")
    if not token:
        raise BatchError(f"auth file has no access token: {auth_path}")
    return hashlib.sha256(token.encode()).hexdigest()


def resolve_existing_account_ids(config: dict[str, str], auth_paths: list[Path]) -> dict[str, int]:
    wanted = {auth_token_hash(path) for path in auth_paths}
    proc = run([
        "docker", "exec", config["SUB2API_POSTGRES_CONTAINER"],
        "psql", "-U", config["SUB2API_PG_USER"], "-d", config["SUB2API_PG_DB"],
        "-At", "-F", "\t", "-c",
        "select id, coalesce(credentials->>'access_token','') from accounts where deleted_at is null and platform='grok' and type='oauth';",
    ])
    if proc.returncode != 0:
        raise BatchError("failed to resolve existing Sub2API accounts")
    found: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2 or not parts[1]:
            continue
        digest = hashlib.sha256(parts[1].encode()).hexdigest()
        if digest in wanted:
            if digest in found:
                raise BatchError("duplicate active Sub2API access-token hash detected")
            found[digest] = int(parts[0])
    return found


def backup_database(config: dict[str, str], backup_dir: Path) -> dict[str, Any]:
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    path = backup_dir / f"pre-grok-reconcile-{stamp}.dump"
    with path.open("wb") as handle:
        proc = subprocess.run([
            "docker", "exec", config["SUB2API_POSTGRES_CONTAINER"],
            "pg_dump", "-U", config["SUB2API_PG_USER"], "-d", config["SUB2API_PG_DB"],
            "-Fc", "--no-owner", "--no-acl",
        ], stdout=handle, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        path.unlink(missing_ok=True)
        raise BatchError("Sub2API backup failed before reconciliation")
    path.chmod(0o600)
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def grok_group_id(config: dict[str, str]) -> int:
    group = config["SUB2API_GROUP"].replace("'", "''")
    proc = run([
        "docker", "exec", config["SUB2API_POSTGRES_CONTAINER"],
        "psql", "-U", config["SUB2API_PG_USER"], "-d", config["SUB2API_PG_DB"], "-Atc",
        f"select id from groups where deleted_at is null and name='{group}' and platform='grok' and status='active' and is_exclusive and require_oauth_only order by id limit 1;",
    ])
    if proc.returncode != 0 or not proc.stdout.strip().isdigit():
        raise BatchError("valid exclusive Grok group not found")
    return int(proc.stdout.strip())


def make_admin_token(config: dict[str, str]) -> str:
    helper_path = Path(config["SUB2API_IMPORT_TOOL"])
    spec = importlib.util.spec_from_file_location("sub2api_live_tool_runtime", helper_path)
    if spec is None or spec.loader is None:
        raise BatchError("cannot load Sub2API import helper for admin authentication")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = SimpleNamespace(
        postgres_container=config["SUB2API_POSTGRES_CONTAINER"],
        pg_user=config["SUB2API_PG_USER"], pg_db=config["SUB2API_PG_DB"], timeout=120,
    )
    try:
        env = module.load_env(Path(config["SUB2API_ENV"]))
        admin = module.get_admin(args)
        return module.make_admin_jwt(env, admin, 600)
    except Exception as exc:
        raise BatchError("failed to create short-lived in-memory Sub2API admin token") from exc


def update_account_via_admin_api(config: dict[str, str], token: str, account_id: int, group_id: int) -> None:
    payload = json.dumps({
        "credentials": {"base_url": config["GROK_ACCOUNT_BASE_URL"].rstrip("/")},
        "group_ids": [group_id],
        "priority": 5,
        "confirm_mixed_channel_risk": True,
    }, separators=(",", ":")).encode()
    request = urllib.request.Request(
        config["SUB2API_URL"].rstrip("/") + f"/api/v1/admin/accounts/{int(account_id)}",
        data=payload, method="PUT",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=120) as response:
            raw = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    if status >= 400:
        raise BatchError(f"Sub2API account update failed for ID {account_id}: HTTP {status}")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BatchError(f"Sub2API account update returned non-JSON for ID {account_id}") from exc
    if parsed.get("code") not in (0, "0"):
        raise BatchError(f"Sub2API account update was rejected for ID {account_id}")


def reconcile_imported_accounts(config: dict[str, str], imported_ids: list[int]) -> None:
    if not imported_ids:
        raise BatchError("no imported account IDs to reconcile")
    group_id = grok_group_id(config)
    token = make_admin_token(config)
    for account_id in sorted(set(imported_ids)):
        update_account_via_admin_api(config, token, account_id, group_id)


def validate_import_output(payload: dict[str, Any], expected: int) -> None:
    response = payload.get("import", {}).get("response", {})
    data = response.get("data", {}) if isinstance(response, dict) else {}
    failed = data.get("account_failed")
    created = data.get("account_created")
    imported_ids = payload.get("imported_ids")
    verification = payload.get("verification", {})
    if failed != 0 or created != expected:
        raise BatchError(f"Sub2API import count mismatch: created={created} failed={failed} expected={expected}")
    if not isinstance(imported_ids, list) or len(imported_ids) != expected:
        raise BatchError("Sub2API imported ID count does not match the bundle")
    if verification.get("imported_active") != expected:
        raise BatchError("Sub2API active-account verification failed")
    if verification.get("imported_bound_group") != expected:
        raise BatchError("Sub2API group-binding verification failed")


def validate_imported_account_state(config: dict[str, str], imported_ids: list[int]) -> list[dict[str, Any]]:
    if not imported_ids or any(not isinstance(item, int) or item < 1 for item in imported_ids):
        raise BatchError("invalid imported account IDs")
    ids = ",".join(str(item) for item in sorted(set(imported_ids)))
    group = config["SUB2API_GROUP"].replace("'", "''")
    sql = f"""
select a.id, a.platform, a.type, a.status, a.schedulable,
       exists (
         select 1 from account_groups ag join groups g on g.id=ag.group_id
         where ag.account_id=a.id and g.deleted_at is null and g.name='{group}'
       ), coalesce(a.credentials->>'base_url',''),
       (select count(*) from account_groups all_ag where all_ag.account_id=a.id)
from accounts a
where a.deleted_at is null and a.id in ({ids})
order by a.id;
"""
    proc = run([
        "docker", "exec", "-i", config["SUB2API_POSTGRES_CONTAINER"],
        "psql", "-U", config["SUB2API_PG_USER"], "-d", config["SUB2API_PG_DB"],
        "-At", "-F", "\t",
    ], input_text=sql)
    if proc.returncode != 0:
        raise BatchError(f"Sub2API exact account verification query failed: exit {proc.returncode}")
    rows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 8:
            continue
        row = {
            "id": int(parts[0]),
            "platform": parts[1],
            "type": parts[2],
            "status": parts[3],
            "schedulable": parts[4] == "t",
            "bound_group": parts[5] == "t",
            "base_url": parts[6],
            "group_count": int(parts[7]),
        }
        rows.append(row)
    if len(rows) != len(set(imported_ids)):
        raise BatchError("Sub2API exact account verification did not return every imported ID")
    invalid = [
        row for row in rows
        if row["platform"] != "grok" or row["type"] != "oauth"
        or row["status"] != "active" or not row["schedulable"] or not row["bound_group"]
        or row["base_url"] != config["GROK_ACCOUNT_BASE_URL"].rstrip("/")
        or row["group_count"] != 1
    ]
    if invalid:
        raise BatchError("Sub2API imported accounts failed Grok group, scheduling, or CLI proxy base URL verification")
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch register accounts and import one combined bundle into Sub2API")
    parser.add_argument("--count", type=int, default=1, help="registration attempts in this batch")
    parser.add_argument("--resume", nargs="?", const="latest", help="resume a failed import without registering again")
    parser.add_argument("--private-dir", default=str(PROJECT_DIR / "private"))
    parser.add_argument("--failure-policy", choices=["abort", "continue"], default="continue")
    parser.add_argument("--max-consecutive-failures", type=int, default=3)
    parser.add_argument("--import-partial", action="store_true", help="import successful accounts even if some attempts failed")
    parser.add_argument("--cleanup-failed-mailboxes", action="store_true")
    parser.add_argument("--confirm-production-write", action="store_true")
    parser.add_argument("--no-import", action="store_true", help="register and build the bundle without writing Sub2API")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.count < 1:
        raise BatchError("--count must be at least 1")
    if args.max_consecutive_failures < 1:
        raise BatchError("--max-consecutive-failures must be at least 1")
    if not args.no_import and not args.confirm_production_write:
        raise BatchError("production import requires --confirm-production-write")

    private_dir = Path(args.private_dir).expanduser().resolve()
    runtime_file = private_dir / "runtime.env"
    config = load_env(runtime_file)
    common_keys = [
        "YESCAPTCHA_API_KEY", "IMAP_SERVER", "IMAP_PASSWORD", "MAILU_DOMAIN",
        "MAILU_DB", "MAILU_ADMIN_CONTAINER", "MAILU_FLASK_BIN",
        "MAILU_IMAP_CONTAINER", "MAILU_MAIL_ROOT", "SUB2API_GROUP", "GROK_ACCOUNT_BASE_URL",
    ]
    require_config(config, common_keys)
    validate_grok_target_config(config)
    if not args.no_import:
        require_config(config, [
            "SUB2API_ENV", "SUB2API_URL", "SUB2API_GROUP",
            "SUB2API_POSTGRES_CONTAINER", "SUB2API_PG_USER", "SUB2API_PG_DB",
            "SUB2API_IMPORT_TOOL", "GROK_ACCOUNT_BASE_URL",
        ])
    if runtime_file.stat().st_mode & 0o077:
        raise BatchError(f"runtime config permissions must be 0600: {runtime_file}")

    if args.resume:
        batch_dir = resolve_resume_batch(private_dir, args.resume)
        manifest, bundle_path, auth_paths = load_resume_bundle(batch_dir)
        manifest_path = batch_dir / "manifest.json"
        failures = int(manifest.get("failed_registrations") or 0)
        atomic_json(bundle_path, build_bundle(auth_paths, config["GROK_ACCOUNT_BASE_URL"].rstrip("/")))
        manifest["bundle_sha256"] = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
        manifest["bundle_normalized_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        manifest["target_group"] = config["SUB2API_GROUP"]
        manifest["target_base_url"] = config["GROK_ACCOUNT_BASE_URL"].rstrip("/")
        manifest.update({"status": "resuming-import", "current_stage": "import-preflight"})
        atomic_json(manifest_path, manifest)
        print(f"Resuming {manifest['batch_id']}: {len(auth_paths)} accounts already registered", flush=True)
    else:
        batch_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
        batch_dir = private_dir / "runs" / batch_id
        for name in ("auth", "results", "logs", "bundle", "import", "backup"):
            (batch_dir / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        manifest = {
            "batch_id": batch_id, "requested_attempts": args.count,
            "production_import_confirmed": bool(args.confirm_production_write and not args.no_import),
            "attempts": [], "status": "running", "current_stage": "registration",
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        manifest_path = batch_dir / "manifest.json"
        atomic_json(manifest_path, manifest)
        auth_paths: list[Path] = []
        failures = 0
        consecutive_failures = 0
        registration_env = os.environ.copy()
        registration_env.update(config)

        for index in range(1, args.count + 1):
            prefix = "xai" + secrets.token_hex(3)
            email = f"{prefix}@{config['MAILU_DOMAIN']}"
            attempt_auth_dir = batch_dir / "auth" / prefix
            attempt_auth_dir.mkdir(mode=0o700)
            result_path = batch_dir / "results" / f"{prefix}.json"
            log_path = batch_dir / "logs" / f"{prefix}.log"
            attempt: dict[str, Any] = {
                "index": index, "email": email, "status": "running", "stage": "mailbox",
                "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            manifest["attempts"].append(attempt)
            atomic_json(manifest_path, manifest)
            print(f"[{index}/{args.count}] creating mailbox {email}", flush=True)
            mailbox_created = False
            started = time.monotonic()
            try:
                create_mailbox(config, email)
                mailbox_created = True
                attempt.update({"mailbox_created": True, "stage": "signup"})
                atomic_json(manifest_path, manifest)
                env = registration_env.copy()
                env.update({"IMAP_EMAIL": email, "IMAP_USERNAME": email, "CLIPROXYAPI_AUTH_DIR": str(attempt_auth_dir)})
                stage_map = {
                    "cookie + scrape OK": "email-verification", "email verified": "turnstile",
                    "Turnstile ": "account-creation", "account created": "sso",
                    "SSO acquired": "oauth", "SSO extraction failed": "oauth",
                    "OAuth Build path": "oauth", "Build OAuth OK": "completed",
                }

                def on_registration_line(line: str) -> None:
                    for marker, stage in stage_map.items():
                        if marker in line:
                            attempt["stage"] = stage
                            atomic_json(manifest_path, manifest)
                            break
                    if line.strip():
                        print(f"  {line}", flush=True)

                proc = run([
                    sys.executable, str(PROJECT_DIR / "run.py"), "-e", "imap",
                    "--cliproxyapi-auth-dir", str(attempt_auth_dir), "--result-json", str(result_path),
                ], env=env, log=log_path, on_line=on_registration_line)
                if proc.returncode != 0:
                    raise BatchError(f"registration process exited with code {proc.returncode}")
                result = load_single_result(result_path, email, attempt_auth_dir)
                auth_path = Path(result["cliproxyapi_auth"])
                auth_paths.append(auth_path)
                attempt.update({
                    "status": "registered", "stage": "completed", "auth_file": str(auth_path),
                    "result_file": str(result_path), "duration_seconds": round(time.monotonic() - started, 1),
                })
                consecutive_failures = 0
                print(f"[{index}/{args.count}] succeeded | total {len(auth_paths)} success, {failures} failed", flush=True)
            except Exception as exc:
                failures += 1
                consecutive_failures += 1
                attempt.update({
                    "status": "failed", "failed_stage": attempt.get("stage"), "error": str(exc),
                    "duration_seconds": round(time.monotonic() - started, 1),
                })
                print(f"[{index}/{args.count}] FAILED at {attempt.get('failed_stage')}: {exc}", file=sys.stderr, flush=True)
                if args.cleanup_failed_mailboxes and mailbox_created:
                    try:
                        created_upstream = upstream_account_created(result_path)
                        if created_upstream is False:
                            delete_mailbox_exact(config, email)
                            attempt["mailbox_rollback"] = "deleted"
                        elif created_upstream is True:
                            attempt["mailbox_rollback"] = "preserved-upstream-account-created"
                        else:
                            attempt["mailbox_rollback"] = "preserved-upstream-state-unknown"
                    except Exception as cleanup_exc:
                        attempt["mailbox_rollback"] = f"failed: {cleanup_exc}"
                if args.failure_policy == "abort" or consecutive_failures >= args.max_consecutive_failures:
                    atomic_json(manifest_path, manifest)
                    break
            finally:
                atomic_json(manifest_path, manifest)

        manifest["successful_registrations"] = len(auth_paths)
        manifest["failed_registrations"] = failures
        if not auth_paths:
            manifest["status"] = "failed-no-successes"
            atomic_json(manifest_path, manifest)
            return 1
        bundle_path = batch_dir / "bundle" / "sub2api-bundle.json"
        atomic_json(bundle_path, build_bundle(auth_paths, config["GROK_ACCOUNT_BASE_URL"].rstrip("/")))
        manifest["bundle"] = str(bundle_path)
        manifest["bundle_sha256"] = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
        manifest["target_group"] = config["SUB2API_GROUP"]
        manifest["target_base_url"] = config["GROK_ACCOUNT_BASE_URL"].rstrip("/")

    if failures and not args.import_partial:
        manifest["status"] = "registration-failed-import-skipped"
        atomic_json(manifest_path, manifest)
        print("Batch has failed registrations; production import skipped. Use --import-partial to override.", file=sys.stderr)
        return 1
    if args.no_import:
        manifest["status"] = "registered-not-imported"
        atomic_json(manifest_path, manifest)
        print(f"DONE: {len(auth_paths)} registrations; bundle saved without production import")
        return 0 if not failures else 1

    import_result_path = batch_dir / "import" / "result.json"
    import_log_path = batch_dir / "import" / "helper.log"
    existing = resolve_existing_account_ids(config, auth_paths)
    missing_auth_paths = [path for path in auth_paths if auth_token_hash(path) not in existing]
    manifest.update({
        "status": "importing", "current_stage": "sub2api-preflight",
        "existing_account_count": len(existing), "missing_account_count": len(missing_auth_paths),
    })
    for key in ("error_summary", "import_exit_code", "completed_at"):
        manifest.pop(key, None)
    atomic_json(manifest_path, manifest)
    import_payload: dict[str, Any]
    if missing_auth_paths:
        pending_bundle_path = batch_dir / "bundle" / "pending-sub2api-bundle.json"
        atomic_json(pending_bundle_path, build_bundle(missing_auth_paths, config["GROK_ACCOUNT_BASE_URL"].rstrip("/")))
        command = [
            sys.executable, config["SUB2API_IMPORT_TOOL"], "import",
            "--bundle", str(pending_bundle_path),
            "--postgres-container", config["SUB2API_POSTGRES_CONTAINER"],
            "--pg-user", config["SUB2API_PG_USER"],
            "--pg-db", config["SUB2API_PG_DB"],
            "--env-file", config["SUB2API_ENV"],
            "--base-url", config["SUB2API_URL"],
            "--backup-dir", str(batch_dir / "backup"),
            "--group", config["SUB2API_GROUP"],
        ]
        manifest["current_stage"] = "sub2api-import"
        atomic_json(manifest_path, manifest)
        print(f"Importing {len(missing_auth_paths)} missing accounts; {len(existing)} already exist", flush=True)
        proc = run(command, env=helper_env(config), log=import_log_path)
        if proc.returncode != 0:
            manifest["status"] = "import-failed"
            manifest["import_exit_code"] = proc.returncode
            manifest["error_summary"] = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "Sub2API helper failed"
            atomic_json(manifest_path, manifest)
            raise BatchError(
                f"Sub2API import failed: {manifest['error_summary']}. "
                f"Resume this batch instead of registering again: --resume {manifest['batch_id']} --confirm-production-write"
            )
        try:
            import_payload = json.loads(proc.stdout)
            validate_import_output(import_payload, len(missing_auth_paths))
        except Exception as exc:
            manifest.update({"status": "import-verification-failed", "error_summary": str(exc)})
            atomic_json(manifest_path, manifest)
            raise BatchError(f"Sub2API helper result verification failed: {exc}") from exc
    else:
        print(f"All {len(auth_paths)} accounts already exist; reconciling the interrupted import", flush=True)
        backup = backup_database(config, batch_dir / "backup")
        import_payload = {"reconciled_existing": True, "backup": backup, "imported_ids": []}

    atomic_json(import_result_path, import_payload)
    resolved = resolve_existing_account_ids(config, auth_paths)
    if len(resolved) != len(auth_paths):
        manifest.update({"status": "import-verification-failed", "error_summary": "not every auth token resolved to an active account"})
        atomic_json(manifest_path, manifest)
        raise BatchError("not every auth token resolved to an active Sub2API account")
    all_ids = sorted(resolved.values())
    manifest["current_stage"] = "grok-reconcile"
    atomic_json(manifest_path, manifest)
    try:
        reconcile_imported_accounts(config, all_ids)
        exact_state = validate_imported_account_state(config, all_ids)
    except Exception as exc:
        manifest.update({"status": "import-verification-failed", "error_summary": str(exc)})
        atomic_json(manifest_path, manifest)
        raise BatchError(f"Sub2API Grok reconciliation failed: {exc}") from exc
    for key in ("error_summary", "import_exit_code"):
        manifest.pop(key, None)
    manifest.update({
        "status": "imported-not-probed",
        "current_stage": "completed",
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "import_result": str(import_result_path),
        "imported_ids": all_ids,
        "backup": import_payload.get("backup", {}),
        "exact_account_state": exact_state,
        "upstream_usability_probes": "not-run",
    })
    atomic_json(manifest_path, manifest)
    print(f"IMPORTED: {len(auth_paths)} accounts are ready in the Grok group; upstream probe not run")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
