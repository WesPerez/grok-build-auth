#!/usr/bin/env python3
"""Verify and close a completed server-protocol Grok registration batch.

The registration orchestrator intentionally keeps recovery material while it is
running.  This tool turns that runtime state into a minimal, token-free batch
index after Sub2API has taken ownership of the refresh chain.
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
import stat
import subprocess
import sys
import time
from typing import Any, Callable
from urllib.parse import urlparse


PROJECT_DIR = Path(__file__).resolve().parents[1]
ORCHESTRATOR_PATH = PROJECT_DIR / "scripts" / "register_and_import.py"
BATCH_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[a-f0-9]{6}$")
GROK_CLI_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
ALLOWED_RUNTIME_STATUSES = {"imported-preprobed", "postimport-account-probe-failed"}
PREPARED_ARTIFACT = "closeout-prepared.json"
PREPARED_TTL_SECONDS = 30 * 60
EXPLICIT_QUOTA_REASONS = {
    "FREE_USAGE_EXHAUSTED",
    "SPENDING_LIMIT_EXCEEDED",
    "INSUFFICIENT_QUOTA",
    "QUOTA_EXHAUSTED",
    "CREDIT_EXHAUSTED",
}
CLEANUP_JOURNAL_STATUSES = {"quarantining", "quarantined", "completed"}
CLEANUP_ENTRY_STATUSES = {"pending", "quarantined", "deleted"}


class FinalizeError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinalizeError(f"invalid JSON file: {path}") from exc
    if not isinstance(payload, dict):
        raise FinalizeError(f"JSON root must be an object: {path}")
    return payload


def load_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise FinalizeError(f"runtime config not found: {path}")
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
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temp.chmod(0o600)
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp.unlink(missing_ok=True)


def fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_batch_dir(private_dir: Path, batch_id: str) -> Path:
    if not BATCH_ID_RE.fullmatch(batch_id):
        raise FinalizeError("batch ID must be an exact timestamp-token identifier")
    runs_candidate = private_dir / "runs"
    if runs_candidate.is_symlink():
        raise FinalizeError("private/runs must not be a symlink")
    runs_root = runs_candidate.resolve()
    candidate = runs_root / batch_id
    if candidate.is_symlink():
        raise FinalizeError("batch directory must not be a symlink")
    batch_dir = candidate.resolve()
    if batch_dir.parent != runs_root or not batch_dir.is_dir():
        raise FinalizeError("batch directory is missing or outside private/runs")
    if batch_dir.name != batch_id or not (batch_dir / "manifest.json").is_file():
        raise FinalizeError("batch directory is not a regular batch")
    return batch_dir


def require_exact_child(path_value: str | Path, parent: Path, *, must_exist: bool = True) -> Path:
    candidate = Path(path_value).expanduser()
    raw_path = Path(os.path.abspath(os.fspath(candidate)))
    raw_root = Path(os.path.abspath(os.fspath(parent.expanduser())))
    try:
        raw_path.relative_to(raw_root)
    except ValueError as exc:
        raise FinalizeError(f"path is outside the batch boundary: {raw_path}") from exc
    current = raw_path
    while True:
        if current.is_symlink():
            raise FinalizeError(f"path component must not be a symlink: {current}")
        if current == raw_root:
            break
        current = current.parent
    path = raw_path.resolve()
    root = raw_root.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise FinalizeError(f"path is outside the batch boundary: {path}") from exc
    if path == root:
        raise FinalizeError("expected a file below the batch boundary")
    if must_exist and (not path.is_file() or path.is_symlink()):
        raise FinalizeError(f"expected a regular file: {path}")
    return path


def acquire_non_destructive_batch_lock(private_dir: Path):
    path = private_dir / "batch-orchestrator.lock"
    if not path.is_file() or path.is_symlink():
        raise FinalizeError("batch lock file is missing or unsafe")
    handle = path.open("r", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise FinalizeError("registration/import batch is still running") from exc
    return handle


def load_orchestrator():
    spec = importlib.util.spec_from_file_location("grok_batch_orchestrator_closeout", ORCHESTRATOR_PATH)
    if spec is None or spec.loader is None:
        raise FinalizeError("cannot load registration orchestrator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def registered_sources(batch_dir: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    attempts = manifest.get("attempts")
    if not isinstance(attempts, list):
        raise FinalizeError("runtime manifest has no attempts list")
    auth_root = batch_dir / "auth"
    result_root = batch_dir / "results"
    log_root = batch_dir / "logs"
    sources: list[dict[str, Any]] = []
    seen_tokens: set[str] = set()
    for attempt in attempts:
        if not isinstance(attempt, dict) or attempt.get("status") != "registered":
            continue
        auth_path = require_exact_child(str(attempt.get("auth_file") or ""), auth_root)
        if auth_path.parent.parent.resolve() != auth_root.resolve():
            raise FinalizeError("auth file is not in a single exact attempt directory")
        auth = load_json(auth_path)
        access_token = str(auth.get("access_token") or "")
        refresh_token = str(auth.get("refresh_token") or "")
        email = str(auth.get("email") or "").strip().lower()
        subject = str(auth.get("sub") or "").strip()
        if not access_token or not refresh_token or not email or not subject:
            raise FinalizeError("registered auth is missing identity or refresh-capable OAuth credentials")
        token_hash = hashlib.sha256(access_token.encode()).hexdigest()
        if token_hash in seen_tokens:
            raise FinalizeError("duplicate access-token hash in registered sources")
        seen_tokens.add(token_hash)

        result_path = require_exact_child(str(attempt.get("result_file") or ""), result_root)
        prefix = auth_path.parent.name
        log_path = require_exact_child(log_root / f"{prefix}.log", log_root)
        sources.append({
            "auth_path": auth_path,
            "auth_file_sha256": sha256_file(auth_path),
            "token_sha256": token_hash,
            "refresh_sha256": hashlib.sha256(refresh_token.encode()).hexdigest(),
            "identity_sha256": hashlib.sha256(email.encode()).hexdigest(),
            "subject_sha256": hashlib.sha256(subject.encode()).hexdigest(),
            "result_path": result_path,
            "log_path": log_path, "log_sha256": sha256_file(log_path),
        })
    if not sources:
        raise FinalizeError("batch has no registered auth sources")
    return sources


def helper_import_evidence(batch_dir: Path) -> dict[str, Any]:
    helper_path = batch_dir / "import" / "helper.log"
    payload = load_json(require_exact_child(helper_path, batch_dir / "import"))
    ids = payload.get("imported_ids")
    response = ((payload.get("import") or {}).get("response") or {}).get("data") or {}
    if (
        not isinstance(ids, list)
        or not ids
        or any(not isinstance(item, int) or item < 1 for item in ids)
        or len(set(ids)) != len(ids)
    ):
        raise FinalizeError("import helper has no exact unique account IDs")
    if response.get("account_created") != len(ids) or response.get("account_failed") != 0:
        raise FinalizeError("import helper does not prove an all-created batch")
    backup = payload.get("backup")
    if not isinstance(backup, dict):
        raise FinalizeError("import helper has no database recovery point")
    return {
        "account_ids": sorted(ids), "action": "created", "created": len(ids), "failed": 0,
        "backup": backup,
    }


def validate_runtime_state(manifest: dict[str, Any]) -> None:
    if manifest.get("status") not in ALLOWED_RUNTIME_STATUSES:
        raise FinalizeError("batch runtime status is not eligible for closeout")
    attempts = manifest.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise FinalizeError("batch has no complete attempt set")
    states: dict[str, int] = {}
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise FinalizeError("batch attempt is not an object")
        state = str(attempt.get("status") or "")
        if state not in {"registered", "failed"}:
            raise FinalizeError(f"batch contains nonterminal attempt state: {state or 'missing'}")
        states[state] = states.get(state, 0) + 1
    requested = int(manifest.get("requested_attempts") or 0)
    if requested != len(attempts):
        raise FinalizeError("batch attempt count is incomplete")
    if int(manifest.get("successful_registrations") or 0) != states.get("registered", 0):
        raise FinalizeError("batch registered count differs from terminal attempts")
    if int(manifest.get("failed_registrations") or 0) != states.get("failed", 0):
        raise FinalizeError("batch failure count differs from terminal attempts")


def bundle_records(batch_dir: Path, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bundle_dir = batch_dir / "bundle"
    if not bundle_dir.is_dir() or bundle_dir.is_symlink():
        raise FinalizeError("token bundle directory is missing or unsafe")
    actual_names = {path.name for path in bundle_dir.iterdir() if path.is_file()}
    expected_names = {"sub2api-bundle.json", "pending-sub2api-bundle.json"}
    if actual_names != expected_names:
        raise FinalizeError("token bundle directory contains an unexpected artifact set")
    expected_tokens = {str(item["token_sha256"]) for item in sources}
    expected_refreshes = {str(item["refresh_sha256"]) for item in sources}
    expected_identities = {
        (str(item["identity_sha256"]), str(item["subject_sha256"])) for item in sources
    }
    records: list[dict[str, Any]] = []
    for name in ("sub2api-bundle.json", "pending-sub2api-bundle.json"):
        path = require_exact_child(bundle_dir / name, bundle_dir)
        payload = load_json(path)
        accounts = payload.get("accounts")
        if not isinstance(accounts, list) or len(accounts) != len(sources):
            raise FinalizeError(f"token bundle has an unexpected account count: {name}")
        tokens: set[str] = set()
        refreshes: set[str] = set()
        identities: set[tuple[str, str]] = set()
        for account in accounts:
            credentials = account.get("credentials") if isinstance(account, dict) else None
            if not isinstance(credentials, dict):
                raise FinalizeError(f"token bundle has invalid credentials: {name}")
            token = str(credentials.get("access_token") or "")
            refresh_token = str(credentials.get("refresh_token") or "")
            email = str(credentials.get("email") or "").strip().lower()
            subject = str(credentials.get("sub") or "").strip()
            if not token or not refresh_token or not email or not subject:
                raise FinalizeError(f"token bundle is missing identity or refresh capability: {name}")
            tokens.add(hashlib.sha256(token.encode()).hexdigest())
            refreshes.add(hashlib.sha256(refresh_token.encode()).hexdigest())
            identities.add((
                hashlib.sha256(email.encode()).hexdigest(),
                hashlib.sha256(subject.encode()).hexdigest(),
            ))
        if (
            tokens != expected_tokens
            or refreshes != expected_refreshes
            or identities != expected_identities
        ):
            raise FinalizeError(f"token bundle contains sources outside the verified handoff: {name}")
        records.append({
            "path": path, "file": f"bundle/{name}", "sha256": sha256_file(path),
        })
    return records


def query_account_rows(config: dict[str, str], account_ids: list[int]) -> dict[int, dict[str, Any]]:
    required = ("SUB2API_POSTGRES_CONTAINER", "SUB2API_PG_USER", "SUB2API_PG_DB")
    if any(not config.get(key) for key in required):
        raise FinalizeError("runtime config is missing Sub2API database metadata")
    ids = ",".join(str(item) for item in account_ids)
    sql = f"""
select a.id, a.platform, a.type, a.status, a.schedulable,
       coalesce(a.credentials->>'base_url',''),
       coalesce(a.credentials->>'access_token',''),
       coalesce(a.credentials->>'refresh_token',''),
       coalesce(a.credentials->>'email',''),
       coalesce(a.credentials->>'sub',''),
       coalesce(a.proxy_id,0),
       (select count(*) from account_groups ag where ag.account_id=a.id),
       exists (
         select 1 from account_groups ag join groups g on g.id=ag.group_id
         where ag.account_id=a.id and g.deleted_at is null
           and g.name='grok' and g.platform='grok'
       ),
       (
         select count(*)
         from accounts other
         where other.deleted_at is null and other.platform='grok'
           and (
             (
               coalesce(a.credentials->>'email','') <> ''
               and lower(coalesce(other.credentials->>'email','')) = lower(a.credentials->>'email')
             )
             or (
               coalesce(a.credentials->>'sub','') <> ''
               and coalesce(other.credentials->>'sub','') = a.credentials->>'sub'
             )
           )
       )
from accounts a
where a.deleted_at is null and a.id in ({ids})
order by a.id;
"""
    proc = subprocess.run([
        "docker", "exec", "-i", config["SUB2API_POSTGRES_CONTAINER"],
        "psql", "-U", config["SUB2API_PG_USER"], "-d", config["SUB2API_PG_DB"],
        "-v", "ON_ERROR_STOP=1", "-At", "-F", "\t",
    ], input=sql, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        raise FinalizeError("exact Sub2API account query failed")
    rows: dict[int, dict[str, Any]] = {}
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 14 or not parts[0].isdigit():
            raise FinalizeError("unexpected exact account query output")
        token = parts[6]
        refresh_token = parts[7]
        row = {
            "id": int(parts[0]), "platform": parts[1], "type": parts[2],
            "status": parts[3], "schedulable": parts[4] == "t",
            "base_url": parts[5],
            "token_sha256": hashlib.sha256(token.encode()).hexdigest() if token else "",
            "refresh_sha256": hashlib.sha256(refresh_token.encode()).hexdigest() if refresh_token else "",
            "identity_sha256": hashlib.sha256(parts[8].strip().lower().encode()).hexdigest() if parts[8] else "",
            "subject_sha256": hashlib.sha256(parts[9].strip().encode()).hexdigest() if parts[9] else "",
            "proxy_id": int(parts[10]), "group_count": int(parts[11]),
            "bound_grok": parts[12] == "t",
            "global_identity_count": int(parts[13]),
        }
        rows[row["id"]] = row
    if set(rows) != set(account_ids):
        raise FinalizeError("exact account query did not return every imported ID")
    return rows


def map_sources_to_accounts(
    sources: list[dict[str, Any]], rows: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    by_hash: dict[str, dict[str, Any]] = {}
    by_identity: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows.values():
        token_hash = str(row["token_sha256"])
        if token_hash:
            if token_hash in by_hash:
                raise FinalizeError("production access-token hashes are duplicated")
            by_hash[token_hash] = row
        identity_key = (str(row["identity_sha256"]), str(row["subject_sha256"]))
        by_identity.setdefault(identity_key, []).append(row)
        if not row.get("refresh_sha256"):
            raise FinalizeError(f"account {row['id']} has no production refresh token")
    mappings: list[dict[str, Any]] = []
    used_ids: set[int] = set()
    for source in sources:
        row = by_hash.get(str(source["token_sha256"]))
        if row is None:
            candidates = by_identity.get(
                (str(source["identity_sha256"]), str(source["subject_sha256"])), []
            )
            if len(candidates) != 1:
                raise FinalizeError("local auth does not map to one stable production identity")
            row = candidates[0]
        if (
            row["identity_sha256"] != source["identity_sha256"]
            or row["subject_sha256"] != source["subject_sha256"]
        ):
            raise FinalizeError("local and production email/subject identities differ")
        if row["id"] in used_ids:
            raise FinalizeError("multiple local auth files map to one production account")
        used_ids.add(row["id"])
        if (
            row["platform"] != "grok" or row["type"] != "oauth"
            or row["status"] != "active" or not row["schedulable"]
            or row["base_url"] != GROK_CLI_BASE_URL
            or row["proxy_id"] != 0 or row["group_count"] != 1 or not row["bound_grok"]
            or row.get("global_identity_count") != 1
        ):
            raise FinalizeError(f"account {row['id']} failed static Grok state verification")
        mappings.append({
            **source, "account_id": row["id"],
            "production_token_match": row["token_sha256"] == source["token_sha256"],
            "production_refresh_match": row["refresh_sha256"] == source["refresh_sha256"],
        })
    if {item["account_id"] for item in mappings} != set(rows):
        raise FinalizeError("local and production account mappings differ")
    return sorted(mappings, key=lambda item: item["account_id"])


def classify_probe_evidence(item: dict[str, Any]) -> dict[str, Any]:
    observed_at = utc_now()
    availability = str(item.get("availability") or "")
    if item.get("status") == 200 and item.get("completed") is True:
        return {
            "classification": "usable", "code": str(item.get("code") or "COMPLETED"),
            "status": int(item.get("status") or 200), "observed_at": observed_at,
        }
    quota_evidence = item.get("quota_evidence")
    if isinstance(quota_evidence, dict):
        quota_status = quota_evidence.get("status")
        quota_reason = quota_evidence.get("reason")
        if (
            quota_status in {402, 429}
            and quota_reason in EXPLICIT_QUOTA_REASONS
            and availability in {"", "usable_exhausted"}
            and item.get("code") in {None, "", "RATE_LIMITED"}
            and item.get("status") in {200, quota_status}
        ):
            return {
                "classification": "usable_exhausted", "code": "RATE_LIMITED",
                "status": quota_status, "quota_reason": quota_reason,
                "observed_at": observed_at,
            }
    error = str(item.get("error") or "").lower()
    explicit_quota = (
        "subscription:free-usage-exhausted" in error
        or "spending-limit-exceeded" in error
        or "insufficient_quota" in error
        or "quota exhausted" in error
        or "run out of credit" in error
        or ("used all the included free usage" in error and "rolling 24-hour window" in error)
    )
    match = re.search(r"(?<!\d)(402|429)(?!\d)", error)
    if match and explicit_quota:
        status = int(match.group(1))
        return {
            "classification": "usable_exhausted", "code": "RATE_LIMITED",
            "status": status, "observed_at": observed_at,
        }
    raise FinalizeError(f"account {item.get('account_id')} has no decisive usable probe")


def classify_probe(item: dict[str, Any]) -> str:
    return str(classify_probe_evidence(item)["classification"])


def classify_probe_results(results: Any, account_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not isinstance(results, list) or len(results) != len(account_ids):
        raise FinalizeError("batch specified-account probes are incomplete")
    classifications: dict[int, dict[str, Any]] = {}
    for item in results:
        if not isinstance(item, dict) or not isinstance(item.get("account_id"), int):
            raise FinalizeError("invalid specified-account probe result")
        account_id = int(item["account_id"])
        if account_id in classifications:
            raise FinalizeError("duplicate specified-account probe result")
        classifications[account_id] = classify_probe_evidence(item)
    if set(classifications) != set(account_ids):
        raise FinalizeError("specified-account probe IDs differ from imported IDs")
    return classifications


def historical_probe_classifications(
    manifest: dict[str, Any], account_ids: list[int],
) -> dict[int, dict[str, Any]]:
    preprobe = manifest.get("preimport_auth_probes") or {}
    if (
        preprobe.get("tested") != len(account_ids)
        or preprobe.get("http_200_completed") != len(account_ids)
        or preprobe.get("passed") is not True
    ):
        raise FinalizeError("batch pre-import probes are incomplete")
    postprobe = manifest.get("postimport_account_probes") or {}
    return classify_probe_results(postprobe.get("results"), account_ids)


def validate_backup_with_pg_restore(config: dict[str, str], path: Path) -> None:
    container = config.get("SUB2API_POSTGRES_CONTAINER", "")
    if not container:
        raise FinalizeError("PostgreSQL container is not configured")
    with path.open("rb") as handle:
        proc = subprocess.run(
            ["docker", "exec", "-i", container, "pg_restore", "-l"],
            stdin=handle, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False,
        )
    if proc.returncode != 0:
        raise FinalizeError(f"backup failed pg_restore list verification: {path.name}")


def backup_records(
    batch_dir: Path, config: dict[str, str], helper_backup: dict[str, Any],
    runtime_manifest: dict[str, Any],
    *, verifier: Callable[[dict[str, str], Path], None] = validate_backup_with_pg_restore,
) -> list[dict[str, Any]]:
    backup_dir = batch_dir / "backup"
    if not backup_dir.is_dir() or backup_dir.is_symlink():
        raise FinalizeError("batch backup directory is missing or unsafe")
    paths = sorted(path for path in backup_dir.iterdir() if path.is_file() and not path.is_symlink())
    if not paths or any(path.suffix != ".dump" for path in paths):
        raise FinalizeError("batch backup directory contains no exact dump set")
    evidence_by_name: dict[str, dict[str, Any]] = {}
    helper_path = require_exact_child(str(helper_backup.get("path") or ""), backup_dir)

    def add_evidence(raw: Any, *, kind: str, source: str) -> None:
        if not isinstance(raw, dict) or not raw.get("path"):
            return
        evidence_path = require_exact_child(str(raw["path"]), backup_dir)
        if evidence_path.name == helper_path.name:
            kind = "pre_sub2api_import"
        existing = evidence_by_name.get(evidence_path.name)
        record = {
            "kind": str(raw.get("kind") or kind), "source": source,
            "bytes": raw.get("bytes"), "sha256": raw.get("sha256"),
            "created_at": raw.get("created_at"),
        }
        if existing and existing["source"] == "import/helper.log":
            return
        if existing and existing["kind"] != record["kind"]:
            raise FinalizeError(f"backup role conflict: {evidence_path.name}")
        evidence_by_name[evidence_path.name] = record

    add_evidence(helper_backup, kind="pre_sub2api_import", source="import/helper.log")
    for item in runtime_manifest.get("backup_history") or []:
        add_evidence(item, kind="pre_grok_reconcile", source="manifest.json:backup_history")
    add_evidence(
        runtime_manifest.get("backup"), kind="pre_grok_reconcile",
        source="manifest.json:backup",
    )

    records = []
    for path in paths:
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise FinalizeError(f"backup permissions must be 0600: {path.name}")
        evidence = evidence_by_name.get(path.name)
        if evidence is None:
            raise FinalizeError(f"backup has no durable role evidence: {path.name}")
        actual_bytes = path.stat().st_size
        actual_sha256 = sha256_file(path)
        if evidence.get("bytes") not in (None, actual_bytes):
            raise FinalizeError(f"backup size differs from recorded evidence: {path.name}")
        if evidence.get("sha256") not in (None, actual_sha256):
            raise FinalizeError(f"backup hash differs from recorded evidence: {path.name}")
        verifier(config, path)
        records.append({
            "kind": evidence["kind"], "source": evidence["source"],
            "file": f"backup/{path.name}", "bytes": actual_bytes,
            "sha256": actual_sha256,
            "created_at": evidence.get("created_at") or dt.datetime.fromtimestamp(
                path.stat().st_mtime, tz=dt.timezone.utc
            ).isoformat().replace("+00:00", "Z"),
            "pg_restore_list_verified": True,
            "pg_restore_list_verified_at": utc_now(),
            "retention_status": "retain_until_explicit_backup_policy",
            "deleted_at": None,
        })
    referenced = next((item for item in records if item["file"] == f"backup/{helper_path.name}"), None)
    if referenced is None:
        raise FinalizeError("import recovery point is not present in the batch backup set")
    if (
        helper_backup.get("bytes") != referenced["bytes"]
        or helper_backup.get("sha256") != referenced["sha256"]
    ):
        raise FinalizeError("import recovery point no longer matches helper evidence")
    return records


def failure_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    stages: dict[str, int] = {}
    count = 0
    for attempt in manifest.get("attempts") or []:
        if not isinstance(attempt, dict) or attempt.get("status") == "registered":
            continue
        if attempt.get("status") != "failed":
            continue
        count += 1
        stage = str(attempt.get("failed_stage") or "unknown")
        stages[stage] = stages.get(stage, 0) + 1
    return {"count": count, "stages": stages, "recovery_material_retained": count}


def group_probe(config: dict[str, str]) -> dict[str, Any]:
    module = load_orchestrator()
    result = module.run_postimport_group_probe(config)
    if not (
        result.get("status") == 200
        and result.get("completed") is True
        and result.get("output_ok") is True
    ):
        raise FinalizeError("live Grok group probe failed")
    return {"status": 200, "completed": True, "output_ok": True}


def live_account_probes(config: dict[str, str], account_ids: list[int]) -> list[dict[str, Any]]:
    module = load_orchestrator()
    payload = module.run_postimport_account_probes(config, account_ids)
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise FinalizeError("live specified-account probe returned no structured results")
    return results


def live_probe_classifications(
    config: dict[str, str], account_ids: list[int],
    probe: Callable[[dict[str, str], list[int]], list[dict[str, Any]]],
    *, attempts: int = 3, retry_delay: float = 5.0,
) -> dict[int, dict[str, Any]]:
    pending = list(account_ids)
    classifications: dict[int, dict[str, Any]] = {}
    for attempt in range(attempts):
        results = probe(config, pending)
        if (
            len(results) != len(pending)
            or {item.get("account_id") for item in results if isinstance(item, dict)} != set(pending)
        ):
            raise FinalizeError("live specified-account probe IDs differ from requested IDs")
        retry_ids: list[int] = []
        for item in results:
            account_id = int(item["account_id"])
            try:
                classifications[account_id] = classify_probe_evidence(item)
            except FinalizeError:
                retry_ids.append(account_id)
        if not retry_ids:
            return classifications
        pending = sorted(retry_ids)
        if attempt + 1 < attempts and retry_delay > 0:
            time.sleep(retry_delay)
    raise FinalizeError(f"account {pending[0]} has no decisive usable probe after retries")


def prepare_closeout(
    batch_dir: Path, config: dict[str, str],
    *, account_query: Callable[[dict[str, str], list[int]], dict[int, dict[str, Any]]] = query_account_rows,
    live_account_probe: Callable[[dict[str, str], list[int]], list[dict[str, Any]]] = live_account_probes,
    live_group_probe: Callable[[dict[str, str]], dict[str, Any]] = group_probe,
    backup_verifier: Callable[[dict[str, str], Path], None] = validate_backup_with_pg_restore,
) -> dict[str, Any]:
    manifest = load_json(batch_dir / "manifest.json")
    if manifest.get("batch_id") != batch_dir.name:
        raise FinalizeError("manifest batch ID does not match its directory")
    validate_runtime_state(manifest)
    sources = registered_sources(batch_dir, manifest)
    evidence = helper_import_evidence(batch_dir)
    account_ids = evidence["account_ids"]
    if len(sources) != len(account_ids):
        raise FinalizeError("registered source count differs from imported account count")
    rows = account_query(config, account_ids)
    mappings = map_sources_to_accounts(sources, rows)
    historical_probe_classifications(manifest, account_ids)
    classifications = live_probe_classifications(config, account_ids, live_account_probe)
    live_group_probe(config)
    backups = backup_records(
        batch_dir, config, evidence["backup"], manifest, verifier=backup_verifier,
    )
    bundles = bundle_records(batch_dir, sources)
    helper_path = require_exact_child(batch_dir / "import" / "helper.log", batch_dir / "import")
    helper_record = {"path": helper_path, "sha256": sha256_file(helper_path)}
    failures = failure_summary(manifest)
    usable = sum(value["classification"] == "usable" for value in classifications.values())
    exhausted = sum(
        value["classification"] == "usable_exhausted" for value in classifications.values()
    )
    completed_at = utc_now()

    accounts = []
    for item in mappings:
        accounts.append({
            "account_id": item["account_id"],
            "identity_sha256": item["identity_sha256"],
            "subject_sha256": item["subject_sha256"],
            "source_sha256": item["auth_file_sha256"],
            "auth_sha256": item["token_sha256"],
            "refresh_sha256": item["refresh_sha256"],
            "production_token_match": item["production_token_match"],
            "production_refresh_present": True,
            "production_refresh_match": item["production_refresh_match"],
            "action": evidence["action"],
            "usability": classifications[item["account_id"]]["classification"],
            "specified_account_probe": classifications[item["account_id"]],
            "platform": "grok", "type": "oauth", "status": "active",
            "schedulable": True, "group": "grok", "group_count": 1,
            "official_base_url": True, "proxy_id": 0,
        })

    summary = {
        "requested_attempts": int(manifest.get("requested_attempts") or len(sources) + failures["count"]),
        "registered": len(sources), "registration_failures": failures["count"],
        "usable": usable, "usable_exhausted": exhausted,
        "final_usable": usable + exhausted,
    }
    sanitized_manifest = {
        "schema_version": 2, "batch_id": batch_dir.name,
        "status": "completed", "completion": "usable-with-exhausted" if exhausted else "usable",
        "mode": "server-full", "registration_backend": manifest.get("registration_backend"),
        "started_at": manifest.get("started_at"), "completed_at": completed_at,
        "summary": summary, "accounts": accounts, "failures": failures,
        "verification": {
            "preimport_tested": len(account_ids), "preimport_passed": len(account_ids),
            "postimport_tested": len(account_ids), "postimport_usable": usable,
            "postimport_usable_exhausted": exhausted,
            "production_static_state_passed": len(account_ids),
            "group_probe": {"status": 200, "completed": True, "output_ok": True},
            "local_to_production_hash_diff": 0, "production_to_local_hash_diff": 0,
        },
        "backups": backups,
        "source_artifacts": {
            "import_helper_sha256": helper_record["sha256"],
            "token_bundles": [
                {"file": item["file"], "sha256": item["sha256"]} for item in bundles
            ],
        },
        "cleanup": {
            "status": "prepared", "oauth_source_files": len(sources),
            "token_bundles": len(bundles), "successful_logs": len(sources),
            "recovery_results_retained": len(sources) + failures["count"],
            "failure_logs_retained": failures["count"],
            "database_backups_retained": len(backups),
            "mailboxes": "retained-for-remint", "production_accounts": "retained-usable",
        },
    }
    return {
        "manifest": sanitized_manifest, "mappings": mappings,
        "account_ids": account_ids, "evidence": evidence,
        "bundles": bundles, "helper_log": helper_record,
    }


def _canonical_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def prepared_artifact_payload(batch_dir: Path, prepared: dict[str, Any]) -> dict[str, Any]:
    prepared_at = utc_now()
    expires_at = (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=PREPARED_TTL_SECONDS)
    ).isoformat().replace("+00:00", "Z")
    payload = {
        "schema_version": 1, "batch_id": batch_dir.name,
        "status": "live-verification-prepared", "prepared_at": prepared_at,
        "expires_at": expires_at, "account_ids": prepared["account_ids"],
        "manifest": prepared["manifest"],
        "evidence": {
            "action": prepared["evidence"]["action"],
            "created": prepared["evidence"]["created"],
            "failed": prepared["evidence"]["failed"],
        },
        "mappings": [{
            **{key: value for key, value in item.items() if key not in {"auth_path", "result_path", "log_path"}},
            "auth_path": str(Path(item["auth_path"]).relative_to(batch_dir)),
            "result_path": str(Path(item["result_path"]).relative_to(batch_dir)),
            "log_path": str(Path(item["log_path"]).relative_to(batch_dir)),
        } for item in prepared["mappings"]],
        "bundles": [{
            **{key: value for key, value in item.items() if key != "path"},
            "path": str(Path(item["path"]).relative_to(batch_dir)),
        } for item in prepared["bundles"]],
        "helper_log": {
            "path": str(Path(prepared["helper_log"]["path"]).relative_to(batch_dir)),
            "sha256": prepared["helper_log"]["sha256"],
        },
    }
    payload["artifact_sha256"] = _canonical_sha256(payload)
    return payload


def write_prepared_artifact(batch_dir: Path, prepared: dict[str, Any]) -> Path:
    path = batch_dir / "import" / PREPARED_ARTIFACT
    atomic_json(path, prepared_artifact_payload(batch_dir, prepared))
    return path


def load_prepared_artifact(batch_dir: Path, *, allow_expired: bool = False) -> dict[str, Any]:
    path = batch_dir / "import" / PREPARED_ARTIFACT
    payload = load_json(require_exact_child(path, batch_dir / "import"))
    expected_hash = str(payload.pop("artifact_sha256", ""))
    if not expected_hash or _canonical_sha256(payload) != expected_hash:
        raise FinalizeError("prepared closeout artifact hash mismatch")
    if payload.get("batch_id") != batch_dir.name or payload.get("status") != "live-verification-prepared":
        raise FinalizeError("prepared closeout artifact has invalid identity or status")
    try:
        expires_at = dt.datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise FinalizeError("prepared closeout artifact has invalid expiry") from exc
    if expires_at <= dt.datetime.now(dt.timezone.utc) and not allow_expired:
        raise FinalizeError("prepared closeout artifact expired; run live verification again")
    payload["artifact_sha256"] = expected_hash
    for item in payload.get("mappings") or []:
        item["auth_path"] = require_exact_child(batch_dir / item["auth_path"], batch_dir, must_exist=False)
        item["result_path"] = require_exact_child(batch_dir / item["result_path"], batch_dir, must_exist=False)
        item["log_path"] = require_exact_child(batch_dir / item["log_path"], batch_dir, must_exist=False)
    for item in payload.get("bundles") or []:
        item["path"] = require_exact_child(batch_dir / item["path"], batch_dir, must_exist=False)
    payload["helper_log"]["path"] = require_exact_child(
        batch_dir / payload["helper_log"]["path"], batch_dir, must_exist=False,
    )
    return payload


def _remove_empty_parent(path: Path, boundary: Path) -> int:
    removed = 0
    current = path
    root = boundary.resolve()
    while current.resolve() != root:
        try:
            current.rmdir()
        except OSError:
            break
        removed += 1
        current = current.parent
    return removed


def enforce_private_permissions(batch_dir: Path) -> None:
    for root, dirs, files in os.walk(batch_dir, followlinks=False):
        root_path = Path(root)
        if root_path.is_symlink():
            raise FinalizeError("symlink found in batch directory")
        root_path.chmod(0o700)
        for name in dirs:
            path = root_path / name
            if path.is_symlink():
                raise FinalizeError("symlink found in batch directory")
            path.chmod(0o700)
        for name in files:
            path = root_path / name
            if path.is_symlink():
                raise FinalizeError("symlink found in batch directory")
            path.chmod(0o600)


def _cleanup_entries(batch_dir: Path, prepared: dict[str, Any]) -> list[dict[str, Any]]:
    raw: list[tuple[str, Path, str]] = []
    for item in prepared["mappings"]:
        auth_path = require_exact_child(item["auth_path"], batch_dir / "auth", must_exist=False)
        log_path = require_exact_child(item["log_path"], batch_dir / "logs", must_exist=False)
        raw.append(("oauth_source", auth_path, str(item["auth_file_sha256"])))
        raw.append(("successful_log", log_path, str(item.get("log_sha256") or "")))
    for item in prepared["bundles"]:
        raw.append((
            "token_bundle",
            require_exact_child(item["path"], batch_dir / "bundle", must_exist=False),
            str(item["sha256"]),
        ))
    helper = prepared["helper_log"]
    raw.append((
        "import_helper",
        require_exact_child(helper["path"], batch_dir / "import", must_exist=False),
        str(helper["sha256"]),
    ))
    entries = []
    for index, (kind, source, expected_hash) in enumerate(raw):
        if not expected_hash:
            if not source.is_file():
                raise FinalizeError(f"cleanup source hash is unavailable: {source}")
            expected_hash = sha256_file(source)
        quarantine = batch_dir / "cleanup" / "quarantine" / f"{index:04d}-{source.name}"
        entries.append({
            "kind": kind, "source": str(source.relative_to(batch_dir)),
            "quarantine": str(quarantine.relative_to(batch_dir)),
            "sha256": expected_hash, "status": "pending",
        })
    return entries


def _validate_cleanup_journal_entries(
    batch_dir: Path, prepared: dict[str, Any], journal: dict[str, Any],
) -> None:
    entries = journal.get("entries")
    expected = _cleanup_entries(batch_dir, prepared)
    if not isinstance(entries, list) or len(entries) != len(expected):
        raise FinalizeError("cleanup journal entries differ from prepared evidence")
    immutable_fields = ("kind", "source", "quarantine", "sha256")
    allowed_fields = {*immutable_fields, "status", "quarantined_at", "deleted_at"}
    for index, (actual, expected_entry) in enumerate(zip(entries, expected, strict=True)):
        if not isinstance(actual, dict):
            raise FinalizeError(f"cleanup journal entry {index} is invalid")
        if set(actual) - allowed_fields:
            raise FinalizeError(f"cleanup journal entry {index} has unexpected fields")
        if any(actual.get(field) != expected_entry[field] for field in immutable_fields):
            raise FinalizeError("cleanup journal entries differ from prepared evidence")
        status_value = actual.get("status")
        if status_value not in CLEANUP_ENTRY_STATUSES:
            raise FinalizeError(f"cleanup journal entry {index} has invalid status")
        for timestamp_field in ("quarantined_at", "deleted_at"):
            timestamp = actual.get(timestamp_field)
            if timestamp is not None and not isinstance(timestamp, str):
                raise FinalizeError(
                    f"cleanup journal entry {index} has invalid {timestamp_field}"
                )


def _cleanup_journal(batch_dir: Path, prepared: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    path = batch_dir / "import" / "cleanup-journal.json"
    if path.exists():
        journal = load_json(path)
        if journal.get("batch_id") != batch_dir.name:
            raise FinalizeError("cleanup journal belongs to a different batch")
        if journal.get("account_ids") != prepared["account_ids"]:
            raise FinalizeError("cleanup journal account IDs differ from prepared evidence")
        if journal.get("schema_version") != 1:
            raise FinalizeError("cleanup journal has unsupported schema")
        if journal.get("status") not in CLEANUP_JOURNAL_STATUSES:
            raise FinalizeError("cleanup journal has invalid status")
        if (
            prepared.get("artifact_sha256")
            and journal.get("prepared_artifact_sha256") != prepared["artifact_sha256"]
        ):
            raise FinalizeError("cleanup journal belongs to different prepared evidence")
        _validate_cleanup_journal_entries(batch_dir, prepared, journal)
        return path, journal
    journal = {
        "schema_version": 1, "batch_id": batch_dir.name,
        "status": "quarantining", "created_at": utc_now(),
        "account_ids": prepared["account_ids"],
        "prepared_artifact_sha256": prepared.get("artifact_sha256"),
        "entries": _cleanup_entries(batch_dir, prepared),
    }
    atomic_json(path, journal)
    return path, journal


def _quarantine_entries(batch_dir: Path, journal_path: Path, journal: dict[str, Any]) -> None:
    quarantine_root = batch_dir / "cleanup" / "quarantine"
    quarantine_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for entry in journal["entries"]:
        if entry["status"] in {"quarantined", "deleted"}:
            continue
        source = require_exact_child(batch_dir / entry["source"], batch_dir, must_exist=False)
        quarantine = require_exact_child(
            batch_dir / entry["quarantine"], batch_dir / "cleanup", must_exist=False,
        )
        if source.is_file():
            if source.is_symlink() or sha256_file(source) != entry["sha256"]:
                raise FinalizeError(f"cleanup source changed after live verification: {entry['source']}")
            quarantine.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(source, quarantine)
            fsync_directory(source.parent)
            fsync_directory(quarantine.parent)
        elif not quarantine.is_file() or sha256_file(quarantine) != entry["sha256"]:
            raise FinalizeError(f"cleanup source and quarantine are both unavailable: {entry['source']}")
        entry["status"] = "quarantined"
        entry["quarantined_at"] = utc_now()
        atomic_json(journal_path, journal)
    journal["status"] = "quarantined"
    atomic_json(journal_path, journal)


def _purge_quarantine(batch_dir: Path, journal_path: Path, journal: dict[str, Any]) -> None:
    for entry in journal["entries"]:
        if entry["status"] == "deleted":
            continue
        quarantine = require_exact_child(
            batch_dir / entry["quarantine"], batch_dir / "cleanup", must_exist=False,
        )
        if quarantine.exists():
            if not quarantine.is_file() or quarantine.is_symlink() or sha256_file(quarantine) != entry["sha256"]:
                raise FinalizeError(f"quarantined cleanup artifact changed: {entry['quarantine']}")
            quarantine.unlink()
            fsync_directory(quarantine.parent)
        entry["status"] = "deleted"
        entry["deleted_at"] = utc_now()
        atomic_json(journal_path, journal)
    journal["status"] = "completed"
    journal["completed_at"] = utc_now()
    atomic_json(journal_path, journal)


def commit_closeout(batch_dir: Path, prepared: dict[str, Any]) -> dict[str, Any]:
    checkpoint_path = batch_dir / "import" / "checkpoint.json"
    handoff_path = batch_dir / "handoff.json"
    journal_path, journal = _cleanup_journal(batch_dir, prepared)
    checkpoint = {
        "schema_version": 2, "batch_id": batch_dir.name,
        "status": "closeout-prepared", "account_ids": prepared["account_ids"],
        "prepared_at": utc_now(), "sources": [{
            "account_id": item["account_id"],
            "auth_file": str(Path(item["auth_path"]).relative_to(batch_dir)),
            "auth_file_sha256": item["auth_file_sha256"],
            "auth_sha256": item["token_sha256"],
            "refresh_sha256": item["refresh_sha256"],
            "identity_sha256": item["identity_sha256"],
            "subject_sha256": item["subject_sha256"],
            "log_file": str(Path(item["log_path"]).relative_to(batch_dir)),
        } for item in prepared["mappings"]],
        "bundles": [{"file": item["file"], "sha256": item["sha256"]} for item in prepared["bundles"]],
        "cleanup_journal": str(journal_path.relative_to(batch_dir)),
    }
    atomic_json(checkpoint_path, checkpoint)
    prepared_handoff = json.loads(json.dumps(prepared["manifest"]))
    prepared_handoff["status"] = "closeout-prepared"
    atomic_json(handoff_path, prepared_handoff)

    _quarantine_entries(batch_dir, journal_path, journal)
    counts = {
        "oauth_source_files": sum(item["kind"] == "oauth_source" for item in journal["entries"]),
        "token_bundles": sum(item["kind"] == "token_bundle" for item in journal["entries"]),
        "successful_logs": sum(item["kind"] == "successful_log" for item in journal["entries"]),
    }
    manifest = prepared["manifest"]
    manifest["cleanup"].update({"status": "quarantined", **counts})
    minimal_result = {
        "schema_version": 2, "batch_id": batch_dir.name,
        "action": prepared["evidence"]["action"], "account_ids": prepared["account_ids"],
        "created": prepared["evidence"]["created"], "failed": prepared["evidence"]["failed"],
        "usable": manifest["summary"]["usable"],
        "usable_exhausted": manifest["summary"]["usable_exhausted"],
        "verification": manifest["verification"], "backups": manifest["backups"],
    }
    checkpoint.update({"status": "quarantined", "cleanup": counts})
    atomic_json(batch_dir / "import" / "result.json", minimal_result)
    atomic_json(batch_dir / "manifest.json", manifest)
    atomic_json(handoff_path, manifest)
    atomic_json(checkpoint_path, checkpoint)
    _purge_quarantine(batch_dir, journal_path, journal)

    removed_dirs = 0
    for directory in (
        batch_dir / "auth", batch_dir / "logs", batch_dir / "bundle",
        batch_dir / "cleanup" / "quarantine", batch_dir / "cleanup",
    ):
        if directory.is_dir():
            removed_dirs += _remove_empty_parent(directory, batch_dir)
    completed_at = utc_now()
    manifest["cleanup"].update({
        "status": "completed", "completed_at": completed_at,
        "empty_directories_removed": removed_dirs,
    })
    checkpoint.update({"status": "completed", "completed_at": completed_at})
    atomic_json(batch_dir / "manifest.json", manifest)
    atomic_json(handoff_path, manifest)
    atomic_json(checkpoint_path, checkpoint)
    enforce_private_permissions(batch_dir)
    return {
        "batch_id": batch_dir.name, "status": "completed",
        "accounts": len(prepared["account_ids"]),
        "usable": manifest["summary"]["usable"],
        "usable_exhausted": manifest["summary"]["usable_exhausted"],
        "cleanup": counts,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify and safely close a Grok protocol batch")
    parser.add_argument("--batch", required=True, help="exact batch ID")
    parser.add_argument("--private-dir", default=str(PROJECT_DIR / "private"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--prepare-live-verification", action="store_true",
        help="run production account/group probes and write a short-lived prepared artifact",
    )
    mode.add_argument(
        "--confirm-cleanup", action="store_true",
        help="consume a valid prepared artifact without rerunning production probes",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    private_dir = Path(args.private_dir).expanduser().resolve()
    batch_dir = resolve_batch_dir(private_dir, args.batch)
    lock = acquire_non_destructive_batch_lock(private_dir)
    try:
        if args.prepare_live_verification:
            config = load_env(private_dir / "runtime.env")
            sub2api_url = urlparse(config.get("SUB2API_URL", ""))
            if (
                config.get("SUB2API_GROUP") != "grok"
                or config.get("GROK_ACCOUNT_BASE_URL", "").rstrip("/") != GROK_CLI_BASE_URL
                or sub2api_url.scheme != "http"
                or sub2api_url.hostname not in {"127.0.0.1", "localhost", "::1"}
                or sub2api_url.username is not None
                or sub2api_url.password is not None
            ):
                raise FinalizeError("runtime config is not the exact Grok production target")
            prepared = prepare_closeout(batch_dir, config)
            artifact_path = write_prepared_artifact(batch_dir, prepared)
            manifest = prepared["manifest"]
            result = {
                "batch_id": batch_dir.name, "status": "live-verification-prepared",
                "accounts": len(prepared["account_ids"]),
                "usable": manifest["summary"]["usable"],
                "usable_exhausted": manifest["summary"]["usable_exhausted"],
                "backups_verified": len(manifest["backups"]),
                "prepared_artifact": str(artifact_path.relative_to(batch_dir)),
                "expires_in_seconds": PREPARED_TTL_SECONDS,
            }
        else:
            journal_exists = (batch_dir / "import" / "cleanup-journal.json").is_file()
            prepared = load_prepared_artifact(batch_dir, allow_expired=journal_exists)
            result = commit_closeout(batch_dir, prepared)
    finally:
        lock.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FinalizeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
