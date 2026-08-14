#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


MAX_BODY = 1024 * 1024


def psql_rows(container: str, user: str, database: str) -> list[dict[str, Any]]:
    query = """
select json_build_object(
  'id', a.id,
  'name', a.name,
  'status', a.status,
  'schedulable', a.schedulable,
  'group_ids', coalesce((select json_agg(ag.group_id order by ag.group_id)
                         from account_groups ag where ag.account_id=a.id), '[]'::json)
)
from accounts a
where a.platform='grok' and a.deleted_at is null
order by a.id;
"""
    proc = subprocess.run(
        ["docker", "exec", container, "psql", "-U", user, "-d", database, "-Atc", query],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("failed to list Grok accounts from PostgreSQL")
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def read_limited(response: Any) -> str:
    try:
        return response.read(MAX_BODY).decode("utf-8", errors="replace")
    except Exception:
        return ""


def test_completed(body: str) -> bool:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        if payload.get("success") is True:
            return True
        data = payload.get("data")
        if isinstance(data, dict) and data.get("success") is True:
            return True
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        value = line[5:].strip()
        if not value or value == "[DONE]":
            continue
        try:
            event = json.loads(value)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "test_complete" and event.get("success") is True:
            return True
    return False


def classify(status: int, body: str, error_type: str = "") -> tuple[str, str]:
    low = (body or "").lower()
    if test_completed(body):
        return "usable", "TEST_COMPLETED"
    if status in (402, 429) or any(value in low for value in (
        "free-usage", "rolling 24", "resource_exhausted", "spending-limit",
        "run out of credit", "quota exhausted",
    )):
        return "usable_exhausted", "RATE_LIMITED"
    if any(value in low for value in (
        "invalid_grant", "refresh token has been revoked", "grok_oauth_token_refresh_failed",
    )):
        return "token_invalid", "REFRESH_REVOKED"
    if status == 401 or "invalid credentials" in low:
        return "transient_error", "TOKEN_REFRESH_REQUIRED"
    if "permission-denied" in low or "access to the chat endpoint is denied" in low:
        return "permission_denied", "PERMISSION_DENIED"
    if status == 403:
        return "transient_error", "UPSTREAM_FORBIDDEN"
    if status == 0 or status >= 500:
        return "transient_error", error_type or f"HTTP_{status}"
    return "unknown_error", f"HTTP_{status}"


def account_test(base_url: str, key: str, account: dict[str, Any], timeout: float) -> dict[str, Any]:
    account_id = int(account["id"])
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/v1/admin/accounts/{account_id}/test",
        data=json.dumps({
            "model_id": "grok-4.6",
            "prompt": "Reply exactly: SUB2API_GROK_AUDIT_OK",
            "mode": "responses",
        }).encode(),
        headers={"x-api-key": key, "Content-Type": "application/json"},
        method="POST",
    )
    status = 0
    body = ""
    error_type = ""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            body = read_limited(response)
    except urllib.error.HTTPError as exc:
        status = int(exc.code or 0)
        body = read_limited(exc)
    except Exception as exc:
        error_type = type(exc).__name__
    category, code = classify(status, body, error_type)
    return {
        "id": account_id,
        "name": str(account.get("name") or ""),
        "status": status,
        "category": category,
        "code": code,
        "schedulable": bool(account.get("schedulable")),
        "group_ids": list(account.get("group_ids") or []),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only audit of every Sub2API Grok account")
    parser.add_argument("--base-url", default="http://127.0.0.1:13080")
    parser.add_argument("--admin-key-file", required=True)
    parser.add_argument("--postgres-container", default="sub2api-prod-postgres")
    parser.add_argument("--pg-user", default="sub2api")
    parser.add_argument("--pg-db", default="sub2api")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    key_path = Path(args.admin_key_file).expanduser().resolve()
    key = key_path.read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError("Sub2API admin key file is empty")
    if os.name != "nt" and key_path.stat().st_mode & 0o077:
        raise RuntimeError("Sub2API admin key file must not be group/world accessible")
    accounts = psql_rows(args.postgres_container, args.pg_user, args.pg_db)
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(account_test, args.base_url, key, account, args.timeout): account
            for account in accounts
        }
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item["id"])

    counts: dict[str, int] = {}
    for item in results:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    report = {
        "generated_at": int(time.time()),
        "tested": len(results),
        "counts": counts,
        "results": results,
    }
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, output)
    print(json.dumps({"tested": len(results), "counts": counts}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
