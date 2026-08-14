#!/usr/bin/env python3
"""Batch x.ai registration and one-shot Sub2API import.

This orchestrator keeps each registration isolated, aggregates successful auth
files into one bundle, and calls the Sub2API import helper exactly once. The
helper therefore creates one database backup per batch, regardless of count.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import sqlite3
import subprocess
import sys
import time
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xconsole_client.proxy_pool import ProxyPoolError, load_proxy_pool
from xconsole_client.proxy_health import check_proxy_pool_health
from xai_build_quota_probe import probe as probe_grok_auth


PROJECT_DIR = Path(__file__).resolve().parents[1]
EMAIL_RE = re.compile(r"^(xai[a-f0-9]{6})@([A-Za-z0-9.-]+)$")
BATCH_LOCK_PATH = PROJECT_DIR / "private" / "batch-orchestrator.lock"
GROK_CLI_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
EXPLICIT_QUOTA_MARKERS = (
    ("subscription:free-usage-exhausted", "FREE_USAGE_EXHAUSTED"),
    ("spending-limit-exceeded", "SPENDING_LIMIT_EXCEEDED"),
    ("insufficient_quota", "INSUFFICIENT_QUOTA"),
    ("quota exhausted", "QUOTA_EXHAUSTED"),
    ("run out of credit", "CREDIT_EXHAUSTED"),
)


class BatchError(RuntimeError):
    pass


def acquire_batch_lock(path: Path = BATCH_LOCK_PATH):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = path.open("a+", encoding="utf-8")
    os.chmod(path, 0o600)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise BatchError("another registration/import batch is already running") from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()} started_at={utc_now()}\n")
    handle.flush()
    return handle


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
    if path.name == "manifest.json" and isinstance(payload, dict):
        payload["updated_at"] = utc_now()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.chmod(0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def set_manifest_stage(
    manifest: dict[str, Any], manifest_path: Path, stage: str, *,
    attempt: dict[str, Any] | None = None,
) -> None:
    now = utc_now()
    if attempt is not None:
        if attempt.get("stage") != stage:
            attempt["stage"] = stage
            attempt["stage_started_at"] = now
        attempt["last_activity_at"] = now
    elif manifest.get("current_stage") != stage:
        manifest["current_stage"] = stage
        manifest["stage_started_at"] = now
    manifest["last_activity_at"] = now
    atomic_json(manifest_path, manifest)


def heartbeat_manifest(
    manifest: dict[str, Any], manifest_path: Path, *,
    attempt: dict[str, Any] | None = None,
) -> None:
    now = utc_now()
    manifest["last_activity_at"] = now
    if attempt is not None:
        attempt["last_activity_at"] = now
    atomic_json(manifest_path, manifest)


def run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    log: Path | None = None,
    input_text: str | None = None,
    on_line: Any = None,
    on_heartbeat: Any = None,
    heartbeat_interval: float = 5.0,
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
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        while True:
            events = selector.select(timeout=heartbeat_interval)
            if not events:
                if child.poll() is None:
                    if on_heartbeat is not None:
                        on_heartbeat()
                    continue
                break
            line = child.stdout.readline()
            if not line:
                break
            output.append(line)
            on_line(line.rstrip("\n"))
        selector.close()
        remainder = child.stdout.read()
        if remainder:
            output.append(remainder)
            for line in remainder.splitlines():
                on_line(line)
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
    auth_root = (batch_dir / "auth").resolve()
    for auth_path in auth_paths:
        if not auth_path.is_file() or auth_root not in auth_path.parents:
            raise BatchError("resume auth file is missing or outside the batch directory")
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
        value != GROK_CLI_BASE_URL
        or parsed.scheme != "https"
        or parsed.hostname != "cli-chat-proxy.grok.com"
        or parsed.port is not None
        or parsed.path != "/v1"
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise BatchError(f"GROK_ACCOUNT_BASE_URL must be {GROK_CLI_BASE_URL}")
    return value


def validate_sub2api_environment(config: dict[str, str]) -> str:
    value = config.get("SUB2API_ENVIRONMENT", "").strip().lower()
    allowed = {"local", "development", "test", "preproduction", "production"}
    if value not in allowed:
        raise BatchError(
            "SUB2API_ENVIRONMENT must be one of local, development, test, "
            "preproduction, production"
        )
    return value


def build_sub2api_import_command(
    config: dict[str, str],
    *,
    bundle_path: Path,
    backup_dir: Path,
    confirm_production_write: bool,
) -> list[str]:
    environment = validate_sub2api_environment(config)
    if environment in {"production", "preproduction"} and not confirm_production_write:
        raise BatchError("production import requires --confirm-production-write")
    command = [
        sys.executable, config["SUB2API_IMPORT_TOOL"], "import",
        "--bundle", str(bundle_path),
        "--postgres-container", config["SUB2API_POSTGRES_CONTAINER"],
        "--pg-user", config["SUB2API_PG_USER"],
        "--pg-db", config["SUB2API_PG_DB"],
        "--environment", environment,
        "--env-file", config["SUB2API_ENV"],
        "--base-url", config["SUB2API_URL"],
        "--backup-dir", str(backup_dir),
        "--group", config["SUB2API_GROUP"],
        "--confirm-write",
    ]
    if confirm_production_write:
        command.append("--confirm-production-write")
    return command


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


def credentials_from_auth(auth: dict[str, Any], account_base_url: str) -> dict[str, Any]:
    expires_at = auth.get("expires_at")
    if not expires_at and auth.get("last_refresh") and auth.get("expires_in"):
        try:
            refreshed = dt.datetime.fromisoformat(str(auth["last_refresh"]).replace("Z", "+00:00"))
            expires_at = (refreshed + dt.timedelta(seconds=int(auth["expires_in"]))).isoformat()
        except (TypeError, ValueError):
            expires_at = None
    return {
        "access_token": str(auth.get("access_token") or ""),
        "refresh_token": auth.get("refresh_token", ""),
        "id_token": auth.get("id_token", ""),
        "token_type": auth.get("token_type", "Bearer"),
        "email": auth.get("email", ""),
        "sub": auth.get("sub", ""),
        "expires_at": expires_at,
        "base_url": account_base_url,
    }


def account_from_auth(auth_path: Path, account_base_url: str) -> dict[str, Any]:
    auth = json.loads(auth_path.read_text(encoding="utf-8"))
    token = str(auth.get("access_token") or "")
    if not token:
        raise BatchError(f"auth file has no access token: {auth_path}")
    return {
        "name": auth.get("email", ""),
        "platform": "grok",
        "type": "oauth",
        "credentials": credentials_from_auth(auth, account_base_url),
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
    with path.open("rb") as handle:
        verify = subprocess.run([
            "docker", "exec", "-i", config["SUB2API_POSTGRES_CONTAINER"],
            "pg_restore", "-l",
        ], stdin=handle, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
    if verify.returncode != 0:
        path.unlink(missing_ok=True)
        raise BatchError("Sub2API backup failed pg_restore list verification")
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "pg_restore_list_verified": True,
        "pg_restore_list_verified_at": utc_now(),
    }


def record_manifest_backup(
    manifest: dict[str, Any], manifest_path: Path, backup: dict[str, Any],
    *, kind: str, source: str,
) -> None:
    required = {"path", "bytes", "sha256"}
    if not isinstance(backup, dict) or not required.issubset(backup):
        raise BatchError("Sub2API backup metadata is incomplete")
    record = dict(backup)
    record.update({
        "kind": kind,
        "source": source,
        "created_at": record.get("created_at") or utc_now(),
        "retention_status": "retain_until_batch_finalized",
        "deleted_at": None,
    })
    history = [
        item for item in (manifest.get("backup_history") or [])
        if isinstance(item, dict) and item.get("sha256") != record["sha256"]
    ]
    manifest["backup"] = record
    manifest["backup_history"] = [record, *history]
    atomic_json(manifest_path, manifest)


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


def update_account_via_admin_api(
    config: dict[str, str], token: str, account_id: int, group_id: int,
    proxy_id: int | None = None, credentials: dict[str, Any] | None = None,
) -> None:
    body: dict[str, Any] = {
        "group_ids": [group_id],
        "priority": 5,
        "confirm_mixed_channel_risk": True,
    }
    if proxy_id is not None:
        body["proxy_id"] = proxy_id
    if credentials is not None:
        body["credentials"] = credentials
    payload = json.dumps(body, separators=(",", ":")).encode()
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


def reconcile_imported_accounts(
    config: dict[str, str], imported_ids: list[int],
    proxy_ids: dict[int, int] | None = None,
) -> None:
    if not imported_ids:
        raise BatchError("no imported account IDs to reconcile")
    group_id = grok_group_id(config)
    token = make_admin_token(config)
    for account_id in sorted(set(imported_ids)):
        update_account_via_admin_api(
            config, token, account_id, group_id,
            (proxy_ids or {}).get(account_id, 0),
        )


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


def validate_imported_account_state(
    config: dict[str, str], imported_ids: list[int],
    expected_proxy_ids: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
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
       (select count(*) from account_groups all_ag where all_ag.account_id=a.id),
       coalesce(a.proxy_id, 0)
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
        if len(parts) != 9:
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
            "proxy_id": int(parts[8]),
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
        or (
            row["id"] in (expected_proxy_ids or {})
            and row["proxy_id"] != (expected_proxy_ids or {})[row["id"]]
        )
    ]
    if invalid:
        raise BatchError("Sub2API imported accounts failed Grok group, scheduling, or CLI proxy base URL verification")
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch register accounts and import one combined bundle into Sub2API")
    parser.add_argument("--count", type=int, default=1, help="registration attempts in this batch")
    parser.add_argument("--workers", type=int, default=None, help="bounded concurrent registrations")
    parser.add_argument(
        "--registration-backend",
        choices=["protocol-yescaptcha", "browser-playwright-edge"],
        default="protocol-yescaptcha",
    )
    parser.add_argument("--resume", nargs="?", const="latest", help="resume a failed import without registering again")
    parser.add_argument("--private-dir", default=str(PROJECT_DIR / "private"))
    parser.add_argument("--failure-policy", choices=["abort", "continue"], default="continue")
    parser.add_argument("--max-consecutive-failures", type=int, default=3)
    parser.add_argument("--import-partial", action="store_true", help="import successful accounts even if some attempts failed")
    parser.add_argument("--cleanup-failed-mailboxes", action="store_true")
    parser.add_argument("--confirm-production-write", action="store_true")
    parser.add_argument("--no-import", action="store_true", help="register and build the bundle without writing Sub2API")
    return parser.parse_args()


def run_preimport_auth_probes(
    auth_paths: list[Path], timeout: float = 60.0,
    proxy_by_file: dict[str, str] | None = None,
    max_wait_seconds: float = 0.0,
    retry_interval_seconds: float = 60.0,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> dict[str, Any]:
    started = monotonic_fn()
    attempts = {path.name: 0 for path in auth_paths}
    final_results: dict[str, dict[str, Any]] = {}
    pending = list(auth_paths)

    while pending:
        retry_paths: list[Path] = []
        for path in pending:
            attempts[path.name] += 1
            try:
                item = probe_grok_auth(
                    path,
                    timeout=timeout,
                    proxy=(proxy_by_file or {}).get(path.name, ""),
                )
            except Exception as exc:
                item = {"file": path.name, "error": f"{type(exc).__name__}: {exc}"}
            item["attempts"] = attempts[path.name]
            final_results[path.name] = item
            status = item.get("status")
            if item.get("error") or status == 403 or (isinstance(status, int) and status >= 500):
                retry_paths.append(path)

        elapsed = monotonic_fn() - started
        remaining = max_wait_seconds - elapsed
        if not retry_paths or remaining <= 0:
            break
        delay = min(retry_interval_seconds, remaining)
        if delay <= 0:
            break
        sleep_fn(delay)
        pending = retry_paths

    results = [final_results[path.name] for path in auth_paths]
    passed = sum(1 for item in results if item.get("status") == 200 and not item.get("error"))
    return {
        "tested": len(results),
        "http_200_completed": passed,
        "passed": passed == len(results),
        "max_wait_seconds": max_wait_seconds,
        "retry_interval_seconds": retry_interval_seconds,
        "elapsed_seconds": round(monotonic_fn() - started, 3),
        "results": results,
    }


def build_preprobe_proxy_map(
    auth_paths: list[Path], attempts: list[dict[str, Any]],
    proxy_pool: Any,
) -> dict[str, str]:
    auth_names = {path.name for path in auth_paths}
    proxy_by_file: dict[str, str] = {}
    healthy_specs = list(proxy_pool.specs)
    healthy_by_ref = {spec.ref: spec for spec in healthy_specs}
    fallback_index = 0
    for attempt in attempts:
        auth_file = Path(str(attempt.get("auth_file") or ""))
        if auth_file.name not in auth_names:
            continue
        ref = str(attempt.get("proxy_ref") or "")
        if proxy_pool.configured and ref and ref != "direct":
            selected = healthy_by_ref.get(ref)
            if selected is None and healthy_specs:
                selected = healthy_specs[fallback_index % len(healthy_specs)]
                fallback_index += 1
                attempt["preprobe_proxy_fallback_from"] = ref
            if selected is not None:
                attempt["preprobe_proxy_ref"] = selected.ref
                proxy_by_file[auth_file.name] = selected.url
    return proxy_by_file


def resolve_group_probe_key(config: dict[str, str]) -> str:
    group = config["SUB2API_GROUP"].replace("'", "''")
    proc = run([
        "docker", "exec", config["SUB2API_POSTGRES_CONTAINER"],
        "psql", "-U", config["SUB2API_PG_USER"], "-d", config["SUB2API_PG_DB"], "-Atc",
        "select k.key from api_keys k join groups g on g.id=k.group_id "
        f"where g.name='{group}' and k.status='active' and k.deleted_at is null "
        "order by k.id desc limit 1;",
    ])
    key = proc.stdout.strip() if proc.returncode == 0 else ""
    if not key:
        raise BatchError(f"no active API key found for group {config['SUB2API_GROUP']}")
    return key


def validate_sub2api_proxy_ids(config: dict[str, str], proxy_pool: Any) -> None:
    if not proxy_pool.configured:
        return
    expected = {int(spec.sub2api_proxy_id) for spec in proxy_pool.specs if spec.sub2api_proxy_id is not None}
    if not expected:
        return
    ids = ",".join(str(item) for item in sorted(expected))
    proc = run([
        "docker", "exec", config["SUB2API_POSTGRES_CONTAINER"],
        "psql", "-U", config["SUB2API_PG_USER"], "-d", config["SUB2API_PG_DB"], "-Atc",
        "select id from proxies "
        f"where id in ({ids}) and status='active' and deleted_at is null "
        "and (expires_at is null or expires_at > now()) order by id;",
    ])
    if proc.returncode != 0:
        raise BatchError("failed to validate Sub2API proxy IDs before registration")
    found = {int(line) for line in proc.stdout.splitlines() if line.strip().isdigit()}
    missing = sorted(expected - found)
    if missing:
        raise BatchError(f"Sub2API proxy IDs are missing, inactive, deleted, or expired: {missing}")


def run_postimport_group_probe(config: dict[str, str], timeout: float = 60.0) -> dict[str, Any]:
    url = config["SUB2API_URL"].rstrip("/") + "/v1/responses"
    request = urllib.request.Request(
        url,
        data=json.dumps({
            "model": "grok-4.6",
            "input": "Reply exactly: IMPORT_OK",
            "max_output_tokens": 64,
            "store": False,
        }).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {resolve_group_probe_key(config)}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {}
    text = ""
    for item in payload.get("output") or []:
        for part in item.get("content") or []:
            if part.get("type") == "output_text":
                text += str(part.get("text") or "")
    return {"status": status, "completed": payload.get("status") == "completed", "output_ok": "IMPORT_OK" in text}


def run_postimport_account_probes(
    config: dict[str, str], account_ids: list[int], timeout: float = 90.0,
) -> dict[str, Any]:
    token = make_admin_token(config)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    results: list[dict[str, Any]] = []
    for account_id in account_ids:
        request = urllib.request.Request(
            config["SUB2API_URL"].rstrip("/") + f"/api/v1/admin/accounts/{account_id}/test",
            data=json.dumps({
                "model_id": "grok-4.6",
                "prompt": "Reply exactly: ACCOUNT_IMPORT_OK",
                "mode": "responses",
            }).encode(),
            method="POST",
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                status = int(response.status)
                raw = response.read(1024 * 1024).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read(4096).decode("utf-8", "replace")
        completed = False
        error = ""
        for line in raw.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "test_complete" and event.get("success") is True:
                completed = True
            if event.get("type") in {"error", "test_error"}:
                error = str(event.get("error") or event.get("message") or "")[:300]
        quota_evidence = explicit_quota_evidence(status, raw)
        availability, code = classify_postimport_account_probe(status, raw, completed)
        result = {
            "account_id": account_id,
            "status": status,
            "completed": completed,
            "error": error,
            "availability": availability,
            "code": code,
        }
        if quota_evidence:
            result["quota_evidence"] = quota_evidence
        results.append(result)
    usable_count = sum(item["availability"] == "usable" for item in results)
    usable_exhausted_count = sum(item["availability"] == "usable_exhausted" for item in results)
    failed_count = len(results) - usable_count - usable_exhausted_count
    return {
        "tested": len(results),
        "passed": failed_count == 0,
        "usable_count": usable_count,
        "usable_exhausted_count": usable_exhausted_count,
        "failed_count": failed_count,
        "results": results,
    }


def explicit_quota_evidence(status: int, body: str) -> dict[str, Any] | None:
    low = (body or "").lower()
    quota_status = status if status in {402, 429} else None
    if quota_status is None:
        match = re.search(r"(?<!\d)(402|429)(?!\d)", low)
        if match:
            quota_status = int(match.group(1))
    if quota_status is None:
        return None
    reason = next((code for marker, code in EXPLICIT_QUOTA_MARKERS if marker in low), None)
    if reason is None and (
        "used all the included free usage" in low
        and "rolling 24-hour window" in low
    ):
        reason = "FREE_USAGE_EXHAUSTED"
    if reason is None:
        return None
    return {"status": quota_status, "reason": reason}


def classify_postimport_account_probe(status: int, body: str, completed: bool) -> tuple[str, str]:
    if completed:
        return "usable", "TEST_COMPLETED"
    low = (body or "").lower()
    if explicit_quota_evidence(status, body):
        return "usable_exhausted", "RATE_LIMITED"
    if any(value in low for value in (
        "invalid_grant", "refresh token has been revoked", "grok_oauth_token_refresh_failed",
    )):
        return "token_invalid", "REFRESH_REVOKED"
    if "permission-denied" in low or "access to the chat endpoint is denied" in low:
        return "permission_denied", "PERMISSION_DENIED"
    if status == 401 or "invalid credentials" in low:
        return "transient_error", "TOKEN_REFRESH_REQUIRED"
    if status == 403:
        return "transient_error", "UPSTREAM_FORBIDDEN"
    if status == 0 or status >= 500:
        return "transient_error", f"HTTP_{status}"
    return "unknown_error", f"HTTP_{status}"


def main() -> int:
    batch_lock = acquire_batch_lock()
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
    try:
        configured_workers = int(config.get("GROK_MAX_REGISTRATION_WORKERS", "2") or "2")
        preprobe_max_wait = int(config.get("GROK_PREIMPORT_PROBE_MAX_WAIT_SECONDS", "900") or "900")
        preprobe_retry_interval = int(config.get("GROK_PREIMPORT_PROBE_RETRY_INTERVAL_SECONDS", "60") or "60")
    except ValueError as exc:
        raise BatchError("worker and pre-import probe settings must be integers") from exc
    workers = args.workers if args.workers is not None else configured_workers
    if configured_workers < 1 or configured_workers > 16:
        raise BatchError("GROK_MAX_REGISTRATION_WORKERS must be between 1 and 16")
    if workers < 1 or workers > configured_workers:
        raise BatchError(f"--workers must be between 1 and {configured_workers}")
    if preprobe_max_wait < 0 or preprobe_max_wait > 3600:
        raise BatchError("GROK_PREIMPORT_PROBE_MAX_WAIT_SECONDS must be between 0 and 3600")
    if preprobe_retry_interval < 1 or preprobe_retry_interval > 300:
        raise BatchError("GROK_PREIMPORT_PROBE_RETRY_INTERVAL_SECONDS must be between 1 and 300")
    if args.registration_backend == "browser-playwright-edge" and workers != 1:
        raise BatchError("browser-playwright-edge requires --workers 1")
    try:
        proxy_pool = load_proxy_pool(config.get("GROK_PROXY_POOL_FILE", ""), config)
    except ProxyPoolError as exc:
        raise BatchError(str(exc)) from exc
    if (
        not proxy_pool.configured
        or proxy_pool.schema_version != 2
        or any(spec.source != "resin" for spec in proxy_pool.specs)
    ):
        raise BatchError("registration requires a version=2 Resin proxy pool")
    bind_proxy_after_import = config.get("GROK_BIND_SUB2API_PROXY_AFTER_IMPORT", "false").lower() in {
        "1", "true", "yes", "on",
    }
    configured_proxy_nodes = proxy_pool.enabled_count
    if proxy_pool.configured:
        if bind_proxy_after_import and any(spec.sub2api_proxy_id is None for spec in proxy_pool.specs):
            raise BatchError(
                "post-import proxy stickiness requires sub2api_proxy_id on every enabled node"
            )
        workers = min(workers, proxy_pool.capacity)
        if workers < 1:
            raise BatchError("configured proxy pool has no lease capacity")
    common_keys = [
        "YESCAPTCHA_API_KEY", "IMAP_SERVER", "IMAP_PASSWORD", "MAILU_DOMAIN",
        "MAILU_DB", "MAILU_ADMIN_CONTAINER", "MAILU_FLASK_BIN",
        "MAILU_IMAP_CONTAINER", "MAILU_MAIL_ROOT", "SUB2API_GROUP", "GROK_ACCOUNT_BASE_URL",
    ]
    require_config(config, common_keys)
    validate_grok_target_config(config)
    if not args.no_import:
        require_config(config, [
            "SUB2API_ENV", "SUB2API_ENVIRONMENT", "SUB2API_URL", "SUB2API_GROUP",
            "SUB2API_POSTGRES_CONTAINER", "SUB2API_PG_USER", "SUB2API_PG_DB",
            "SUB2API_IMPORT_TOOL", "GROK_ACCOUNT_BASE_URL",
        ])
        validate_sub2api_environment(config)
        if bind_proxy_after_import:
            validate_sub2api_proxy_ids(config, proxy_pool)
    proxy_health = None
    if proxy_pool.configured:
        try:
            proxy_health = check_proxy_pool_health(
                proxy_pool,
                attempts=int(config.get("GROK_PROXY_HEALTH_ATTEMPTS", "3") or "3"),
                timeout=float(config.get("GROK_PROXY_HEALTH_TIMEOUT", "10") or "10"),
                require_all=False,
            )
            proxy_pool = proxy_pool.only_refs(set(proxy_health.healthy_refs))
            workers = min(workers, proxy_pool.capacity)
        except (ProxyPoolError, ValueError) as exc:
            raise BatchError(f"proxy health preflight failed: {exc}") from exc
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
        manifest["status"] = "resuming-import"
        set_manifest_stage(manifest, manifest_path, "import-preflight")
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
            "started_at": utc_now(), "last_activity_at": utc_now(),
            "stage_started_at": utc_now(),
            "workers": workers,
            "registration_backend": args.registration_backend,
            "preimport_probe_policy": {
                "max_wait_seconds": preprobe_max_wait,
                "retry_interval_seconds": preprobe_retry_interval,
                "retry_statuses": [403, "transport-error", "5xx"],
            },
            "proxy_pool": {
                "configured": proxy_pool.configured,
                "enabled_nodes": proxy_pool.enabled_count,
                "configured_nodes": configured_proxy_nodes,
                "postimport_stickiness": bind_proxy_after_import,
                "mode": "resin",
            },
        }
        if proxy_health is not None:
            manifest["proxy_health"] = {
                "checked_at": proxy_health.checked_at,
                "healthy_refs": list(proxy_health.healthy_refs),
                "results": list(proxy_health.results),
                "snapshot_sha256": proxy_health.snapshot_sha256,
                "policy": "fail-closed",
            }
        manifest_path = batch_dir / "manifest.json"
        atomic_json(manifest_path, manifest)
        auth_paths: list[Path] = []
        failures = 0
        consecutive_failures = 0
        registration_env = os.environ.copy()
        registration_env.update(config)

        manifest_lock = threading.Lock()

        def save_manifest() -> None:
            with manifest_lock:
                atomic_json(manifest_path, manifest)

        def execute_attempt(index: int, attempt: dict[str, Any], prefix: str) -> dict[str, Any]:
            email = f"{prefix}@{config['MAILU_DOMAIN']}"
            attempt_auth_dir = batch_dir / "auth" / prefix
            attempt_auth_dir.mkdir(mode=0o700)
            result_path = batch_dir / "results" / f"{prefix}.json"
            log_path = batch_dir / "logs" / f"{prefix}.log"
            attempt.update(status="running", last_activity_at=utc_now(), stage_started_at=utc_now())
            print(f"[{index}/{args.count}] creating mailbox {email}", flush=True)
            mailbox_created = False
            started = time.monotonic()
            lease = None
            try:
                lease = proxy_pool.acquire()
                attempt["proxy_ref"] = lease.ref if lease else "direct"
                if bind_proxy_after_import and lease and lease.spec.sub2api_proxy_id is not None:
                    attempt["sub2api_proxy_id"] = lease.spec.sub2api_proxy_id
                save_manifest()
                create_mailbox(config, email)
                mailbox_created = True
                attempt["mailbox_created"] = True
                with manifest_lock:
                    set_manifest_stage(manifest, manifest_path, "signup", attempt=attempt)
                env = registration_env.copy()
                env.update({"IMAP_EMAIL": email, "IMAP_USERNAME": email, "CLIPROXYAPI_AUTH_DIR": str(attempt_auth_dir)})
                if lease:
                    env["GROK_ATTEMPT_PROXY_URL"] = lease.url
                stage_map = {
                    "cookie + scrape OK": "email-verification", "email verified": "turnstile",
                    "Turnstile ": "account-creation", "account created": "sso",
                    "browser-stage=signup": "signup",
                    "browser-stage=email-verification": "email-verification",
                    "browser-stage=profile": "account-creation",
                    "browser-stage=turnstile": "turnstile",
                    "browser-stage=sso": "sso",
                    "SSO acquired": "oauth", "SSO extraction failed": "oauth",
                    "OAuth Build path": "oauth", "Build OAuth OK": "completed",
                }

                def on_registration_line(line: str) -> None:
                    with manifest_lock:
                        if line.strip():
                            heartbeat_manifest(manifest, manifest_path, attempt=attempt)
                        for marker, stage in stage_map.items():
                            if marker in line:
                                set_manifest_stage(manifest, manifest_path, stage, attempt=attempt)
                                break
                    if line.strip():
                        print(f"  {line}", flush=True)

                def on_registration_heartbeat() -> None:
                    with manifest_lock:
                        heartbeat_manifest(manifest, manifest_path, attempt=attempt)

                registration_command = [
                    sys.executable, str(PROJECT_DIR / "run.py"), "-e", "imap",
                    "--cliproxyapi-auth-dir", str(attempt_auth_dir), "--result-json", str(result_path),
                    "--proxy-env", "GROK_ATTEMPT_PROXY_URL",
                    "--registration-backend", args.registration_backend,
                ]
                if args.registration_backend == "browser-playwright-edge" and config.get("GROK_BROWSER_HEADED", "true").lower() in {"1", "true", "yes", "on"}:
                    registration_command.append("--browser-headed")
                    registration_command = ["xvfb-run", "-a", *registration_command]
                proc = run(registration_command, env=env, log=log_path, on_line=on_registration_line,
                    on_heartbeat=on_registration_heartbeat)
                if proc.returncode != 0:
                    raise BatchError(f"registration process exited with code {proc.returncode}")
                result = load_single_result(result_path, email, attempt_auth_dir)
                auth_path = Path(result["cliproxyapi_auth"])
                attempt.update({
                    "status": "registered", "stage": "completed", "auth_file": str(auth_path),
                    "result_file": str(result_path), "duration_seconds": round(time.monotonic() - started, 1),
                    "last_activity_at": utc_now(), "completed_at": utc_now(),
                })
                return {"ok": True, "auth_path": auth_path, "index": index}
            except Exception as exc:
                attempt.update({
                    "status": "failed", "failed_stage": attempt.get("stage"), "error": str(exc),
                    "duration_seconds": round(time.monotonic() - started, 1),
                    "last_activity_at": utc_now(), "completed_at": utc_now(),
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
                return {"ok": False, "index": index, "error": str(exc)}
            finally:
                proxy_pool.release(lease)
                save_manifest()

        next_index = 1
        pending: dict[Any, int] = {}
        aborted = False
        with ThreadPoolExecutor(max_workers=workers) as executor:
            while next_index <= args.count and len(pending) < workers:
                prefix = "xai" + secrets.token_hex(3)
                attempt = {"index": next_index, "email": f"{prefix}@{config['MAILU_DOMAIN']}", "status": "queued", "stage": "mailbox", "started_at": utc_now(), "last_activity_at": utc_now(), "stage_started_at": utc_now()}
                manifest["attempts"].append(attempt)
                pending[executor.submit(execute_attempt, next_index, attempt, prefix)] = next_index
                next_index += 1
            save_manifest()
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    pending.pop(future)
                    outcome = future.result()
                    if outcome["ok"]:
                        auth_paths.append(outcome["auth_path"])
                        consecutive_failures = 0
                    else:
                        failures += 1
                        consecutive_failures += 1
                    if ((args.failure_policy == "abort" and not outcome["ok"])
                            or consecutive_failures >= args.max_consecutive_failures):
                        aborted = True
                        manifest["abort_reason"] = "failure-policy-abort" if args.failure_policy == "abort" else "max-consecutive-failures"
                    if not aborted and next_index <= args.count:
                        prefix = "xai" + secrets.token_hex(3)
                        attempt = {"index": next_index, "email": f"{prefix}@{config['MAILU_DOMAIN']}", "status": "queued", "stage": "mailbox", "started_at": utc_now(), "last_activity_at": utc_now(), "stage_started_at": utc_now()}
                        manifest["attempts"].append(attempt)
                        pending[executor.submit(execute_attempt, next_index, attempt, prefix)] = next_index
                        next_index += 1
                save_manifest()
        if aborted:
            manifest["unstarted_attempts"] = max(0, args.count - len(manifest["attempts"]))

        manifest["successful_registrations"] = len(auth_paths)
        manifest["failed_registrations"] = failures
        if not auth_paths:
            manifest["status"] = "failed-no-successes"
            manifest.setdefault(
                "error_summary",
                f"No registrations succeeded; {manifest.get('unstarted_attempts', 0)} attempts were not started",
            )
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

    set_manifest_stage(manifest, manifest_path, "upstream-preprobe")
    proxy_by_file = build_preprobe_proxy_map(
        auth_paths, manifest.get("attempts") or [], proxy_pool,
    )
    preprobe = run_preimport_auth_probes(
        auth_paths,
        proxy_by_file=proxy_by_file,
        max_wait_seconds=preprobe_max_wait,
        retry_interval_seconds=preprobe_retry_interval,
    )
    manifest["preimport_auth_probes"] = preprobe
    atomic_json(manifest_path, manifest)
    if not preprobe["passed"]:
        failed_files = {
            str(item.get("file") or "")
            for item in preprobe["results"]
            if item.get("status") != 200 or item.get("error")
        }
        if args.import_partial:
            for attempt in manifest.get("attempts") or []:
                auth_file = Path(str(attempt.get("auth_file") or ""))
                if auth_file.name in failed_files:
                    attempt.update({
                        "status": "probe-failed",
                        "failed_stage": "upstream-preprobe",
                        "error": "Grok auth probe did not return HTTP 200",
                    })
            auth_paths = [path for path in auth_paths if path.name not in failed_files]
            if not auth_paths:
                manifest.update({"status": "preimport-probe-failed", "error_summary": "No auth probes passed"})
                atomic_json(manifest_path, manifest)
                raise BatchError(manifest["error_summary"])
            bundle_path = batch_dir / "bundle" / "sub2api-bundle.json"
            atomic_json(bundle_path, build_bundle(auth_paths, config["GROK_ACCOUNT_BASE_URL"].rstrip("/")))
            manifest.update({
                "bundle": str(bundle_path),
                "bundle_sha256": hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
                "successful_registrations": len(auth_paths),
                "failed_registrations": len(failed_files),
                "probe_quarantined": sorted(failed_files),
            })
            atomic_json(manifest_path, manifest)
        else:
            manifest.update({
                "status": "preimport-probe-failed",
                "error_summary": f"Only {preprobe['http_200_completed']}/{preprobe['tested']} auth probes returned HTTP 200",
            })
            atomic_json(manifest_path, manifest)
            raise BatchError(manifest["error_summary"])

    import_result_path = batch_dir / "import" / "result.json"
    import_log_path = batch_dir / "import" / "helper.log"
    existing = resolve_existing_account_ids(config, auth_paths)
    missing_auth_paths = [path for path in auth_paths if auth_token_hash(path) not in existing]
    manifest.update({
        "status": "importing",
        "existing_account_count": len(existing), "missing_account_count": len(missing_auth_paths),
    })
    for key in ("error_summary", "import_exit_code", "completed_at"):
        manifest.pop(key, None)
    set_manifest_stage(manifest, manifest_path, "sub2api-preflight")
    import_payload: dict[str, Any]
    if missing_auth_paths:
        pending_bundle_path = batch_dir / "bundle" / "pending-sub2api-bundle.json"
        atomic_json(pending_bundle_path, build_bundle(missing_auth_paths, config["GROK_ACCOUNT_BASE_URL"].rstrip("/")))
        command = build_sub2api_import_command(
            config,
            bundle_path=pending_bundle_path,
            backup_dir=batch_dir / "backup",
            confirm_production_write=args.confirm_production_write,
        )
        set_manifest_stage(manifest, manifest_path, "sub2api-import")
        print(f"Importing {len(missing_auth_paths)} missing accounts; {len(existing)} already exist", flush=True)
        proc = run(
            command, env=helper_env(config), log=import_log_path,
            on_line=lambda line: heartbeat_manifest(manifest, manifest_path),
            on_heartbeat=lambda: heartbeat_manifest(manifest, manifest_path),
        )
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

    backup = import_payload.get("backup")
    if missing_auth_paths:
        record_manifest_backup(
            manifest, manifest_path, backup,
            kind="pre_sub2api_import", source="sub2api_import_helper",
        )
    else:
        record_manifest_backup(
            manifest, manifest_path, backup,
            kind="pre_grok_reconcile", source="register_and_import.backup_database",
        )
    atomic_json(import_result_path, import_payload)
    resolved = resolve_existing_account_ids(config, auth_paths)
    if len(resolved) != len(auth_paths):
        manifest.update({"status": "import-verification-failed", "error_summary": "not every auth token resolved to an active account"})
        atomic_json(manifest_path, manifest)
        raise BatchError("not every auth token resolved to an active Sub2API account")
    all_ids = sorted(resolved.values())
    proxy_ids_by_account: dict[int, int] = {}
    for attempt in manifest.get("attempts") or []:
        if attempt.get("status") != "registered" or not attempt.get("auth_file"):
            continue
        proxy_id = attempt.get("sub2api_proxy_id") if bind_proxy_after_import else None
        if not isinstance(proxy_id, int) or proxy_id < 1:
            continue
        digest = auth_token_hash(Path(str(attempt["auth_file"])))
        account_id = resolved.get(digest)
        if account_id is not None:
            proxy_ids_by_account[account_id] = proxy_id
    if bind_proxy_after_import:
        missing_proxy_bindings = sorted(set(all_ids) - set(proxy_ids_by_account))
        if missing_proxy_bindings:
            raise BatchError(
                "post-import proxy stickiness is enabled but no proxy mapping was recorded "
                f"for account IDs: {missing_proxy_bindings}"
            )
    else:
        # Sub2API interprets proxy_id=0 as an explicit request to clear the binding.
        proxy_ids_by_account = {account_id: 0 for account_id in all_ids}
    set_manifest_stage(manifest, manifest_path, "grok-reconcile")
    try:
        reconcile_imported_accounts(config, all_ids, proxy_ids_by_account)
        exact_state = validate_imported_account_state(config, all_ids, proxy_ids_by_account)
    except Exception as exc:
        manifest.update({"status": "import-verification-failed", "error_summary": str(exc)})
        atomic_json(manifest_path, manifest)
        raise BatchError(f"Sub2API Grok reconciliation failed: {exc}") from exc
    set_manifest_stage(manifest, manifest_path, "sub2api-account-postprobe")
    account_postprobes = run_postimport_account_probes(config, all_ids)
    manifest["postimport_account_probes"] = account_postprobes
    atomic_json(manifest_path, manifest)
    if not account_postprobes["passed"]:
        manifest.update({
            "status": "postimport-account-probe-failed",
            "error_summary": "one or more imported Grok accounts failed their specified-account probe",
        })
        atomic_json(manifest_path, manifest)
        raise BatchError(manifest["error_summary"])
    set_manifest_stage(manifest, manifest_path, "sub2api-postprobe")
    postprobe = run_postimport_group_probe(config)
    manifest["postimport_group_probe"] = postprobe
    atomic_json(manifest_path, manifest)
    if not (postprobe.get("status") == 200 and postprobe.get("completed") and postprobe.get("output_ok")):
        manifest.update({"status": "postimport-probe-failed", "error_summary": f"Sub2API group probe failed: {postprobe}"})
        atomic_json(manifest_path, manifest)
        raise BatchError(manifest["error_summary"])
    for key in ("error_summary", "import_exit_code"):
        manifest.pop(key, None)
    preprobe = manifest.get("preimport_auth_probes") or {}
    preprobe_ok = (
        preprobe.get("http_200_completed") == len(auth_paths)
    )
    manifest.update({
        "status": "imported-preprobed" if preprobe_ok else "imported-not-probed",
        "current_stage": "completed",
        "completed_at": utc_now(), "last_activity_at": utc_now(),
        "import_result": str(import_result_path),
        "imported_ids": all_ids,
        "exact_account_state": exact_state,
        "upstream_usability_probes": "preimport-passed; account-postimport-passed; group-postimport-passed",
    })
    atomic_json(manifest_path, manifest)
    probe_note = "pre-import auth, specified-account, and group post-import probes passed"
    print(f"IMPORTED: {len(auth_paths)} accounts are ready in the Grok group; {probe_note}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
