#!/usr/bin/env python3
"""Bind real Grok OAuth 429 accounts to egress proxies without probes.

Subcommands:
  plan   Paginate Admin API, select 429 candidates, balance proxy quotas, write plan.
  apply  Verify plan, backup Postgres once, bind proxy_id per account with rollback.

Hard constraints:
  - No /test, no generation probes, no DELETE temp, no clear rate_limit/temp/overload.
  - Successful outcome proves binding_applied only; never claims quota recovered.
  - API errors never echo body/key/token/proxy credentials/public IPs.
  - No hardcoded group/proxy/base URL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


TOOL_NAME = "bind_quota_egress"
PLAN_VERSION = 1
DEFAULT_PAGE_SIZE = 100
DEFAULT_FAILURE_LIMIT = 0  # 0 = continue all; >0 stop after N account failures
SENSITIVE_HINTS = (
    "token",
    "password",
    "secret",
    "authorization",
    "api_key",
    "apikey",
    "cookie",
    "credential",
    "proxy_url",
    "private_key",
)
QUOTA_SNAPSHOT_KEYS = (
    "requests",
    "tokens",
    "retry_after_seconds",
    "subscription_tier",
    "entitlement_status",
    "status_code",
    "headers_observed",
    "observation_source",
    "last_probe_at",
    "last_headers_seen_at",
    "updated_at",
)
PRESERVED_STATUS_FIELDS = (
    "rate_limit_reset_at",
    "rate_limited_at",
    "overload_until",
    "temp_unschedulable_until",
    "temp_unschedulable_reason",
)


class ToolError(RuntimeError):
    """User-facing tool failure with a safe message."""


# ---------------------------------------------------------------------------
# Time / JSON / filesystem helpers
# ---------------------------------------------------------------------------


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def ensure_dir_0700(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o700:
        raise ToolError(f"directory mode is {oct(mode)}, expected 0o700: {path}")


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    path = path.resolve()
    ensure_dir_0700(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
        os.chmod(path, mode)
        # fsync directory entry
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_bytes(path, payload.encode("utf-8"), mode=mode)


def append_jsonl(path: Path, value: Any, mode: int = 0o600) -> None:
    path = path.resolve()
    ensure_dir_0700(path.parent)
    line = json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(str(path), flags, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, mode)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ---------------------------------------------------------------------------
# Safe errors / redaction
# ---------------------------------------------------------------------------


def safe_error_message(exc: BaseException) -> str:
    if isinstance(exc, ToolError):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} for {exc.reason or 'request'}"
    if isinstance(exc, urllib.error.URLError):
        return "network error contacting Admin API"
    text = str(exc)
    lowered = text.lower()
    for hint in SENSITIVE_HINTS:
        if hint in lowered:
            return "request failed (details redacted)"
    # Avoid dumping long bodies or obvious IPs.
    if len(text) > 180:
        return text[:180] + "..."
    return text or exc.__class__.__name__


def normalized_url_origin(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    scheme = parsed.scheme.strip().lower()
    host = (parsed.hostname or "").strip().lower()
    if scheme not in {"http", "https"} or not host:
        raise ToolError("base URL must include an http(s) origin")
    if parsed.username is not None or parsed.password is not None:
        raise ToolError("base URL must not contain credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ToolError("base URL contains an invalid port") from exc
    normalized_port = port or (443 if scheme == "https" else 80)
    host_text = f"[{host}]" if ":" in host else host
    return f"{scheme}://{host_text}:{normalized_port}"


# ---------------------------------------------------------------------------
# Account field helpers
# ---------------------------------------------------------------------------


def as_int(value: Any) -> int | None:
    if value is None or value is False:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def is_proxy_empty(proxy_id: Any) -> bool:
    value = as_int(proxy_id)
    return value is None or value == 0


def is_parent_empty(parent_account_id: Any) -> bool:
    value = as_int(parent_account_id)
    return value is None or value == 0


def account_group_ids(account: Mapping[str, Any]) -> list[int]:
    raw = account.get("group_ids")
    ids: list[int] = []
    if isinstance(raw, list):
        for item in raw:
            num = as_int(item)
            if num is not None:
                ids.append(num)
    groups = account.get("account_groups")
    if isinstance(groups, list):
        for item in groups:
            if isinstance(item, Mapping):
                num = as_int(item.get("group_id"))
                if num is not None:
                    ids.append(num)
    # de-dupe preserve order
    seen: set[int] = set()
    out: list[int] = []
    for num in ids:
        if num not in seen:
            seen.add(num)
            out.append(num)
    return out


def in_group(account: Mapping[str, Any], group_id: int) -> bool:
    return group_id in account_group_ids(account)


def grok_snapshot(account: Mapping[str, Any]) -> dict[str, Any] | None:
    extra = account.get("extra")
    if not isinstance(extra, Mapping):
        return None
    snap = extra.get("grok_usage_snapshot")
    if isinstance(snap, Mapping):
        return dict(snap)
    return None


def non_secret_grok_snapshot(account: Mapping[str, Any]) -> dict[str, Any]:
    """Return the complete known quota snapshot shape without arbitrary extra keys."""
    snapshot = grok_snapshot(account) or {}
    return {
        key: json.loads(json.dumps(snapshot[key], ensure_ascii=False))
        for key in QUOTA_SNAPSHOT_KEYS
        if key in snapshot
    }


def snapshot_status_code(account: Mapping[str, Any]) -> int | None:
    snap = grok_snapshot(account)
    if snap is None:
        return None
    return as_int(snap.get("status_code"))


def rate_limit_reset_present(account: Mapping[str, Any]) -> bool:
    value = account.get("rate_limit_reset_at")
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def non_secret_before(account: Mapping[str, Any]) -> dict[str, Any]:
    """Record only non-secret fields needed for audit / resume."""
    return {
        "id": as_int(account.get("id")),
        "platform": account.get("platform"),
        "type": account.get("type"),
        "status": account.get("status"),
        "schedulable": bool(account.get("schedulable")),
        "proxy_id": as_int(account.get("proxy_id")),
        "parent_account_id": as_int(account.get("parent_account_id")),
        "group_ids": account_group_ids(account),
        "rate_limit_reset_at": account.get("rate_limit_reset_at"),
        "rate_limited_at": account.get("rate_limited_at"),
        "overload_until": account.get("overload_until"),
        "temp_unschedulable_until": account.get("temp_unschedulable_until"),
        "temp_unschedulable_reason": account.get("temp_unschedulable_reason"),
        "error_message": None,  # never store raw error text (may leak)
        "updated_at": account.get("updated_at"),
        "grok_usage_snapshot": non_secret_grok_snapshot(account),
    }


def frozen_before_fields(account: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize plan-frozen fields for drift checks before any mutation."""
    before = non_secret_before(account) if "extra" in account else dict(account)
    snapshot = before.get("grok_usage_snapshot")
    status_code = as_int(snapshot.get("status_code")) if isinstance(snapshot, Mapping) else None
    proxy_id = as_int(before.get("proxy_id"))
    parent_id = as_int(before.get("parent_account_id"))
    raw_groups = before.get("group_ids")
    groups = sorted(
        {
            value
            for value in (as_int(item) for item in raw_groups or [])
            if value is not None
        }
    )
    return {
        "platform": str(before.get("platform") or "").lower(),
        "type": str(before.get("type") or "").lower(),
        "status": str(before.get("status") or "").lower(),
        "schedulable": before.get("schedulable") is True,
        "proxy_id": 0 if proxy_id is None else proxy_id,
        "group_ids": groups,
        "parent_account_id": 0 if parent_id is None else parent_id,
        "snapshot_status_code": status_code,
        "rate_limit_reset_at": before.get("rate_limit_reset_at"),
    }


def preserved_status_state(account: Mapping[str, Any]) -> dict[str, Any]:
    """Fields proxy/schedulable writes must leave unchanged."""
    state = {
        "group_ids": sorted(account_group_ids(account)),
        "parent_account_id": as_int(account.get("parent_account_id")),
        "grok_usage_snapshot": non_secret_grok_snapshot(account),
    }
    for field in PRESERVED_STATUS_FIELDS:
        state[field] = account.get(field)
    return state


def candidate_reason(account: Mapping[str, Any], group_id: int) -> str | None:
    """Return rejection reason, or None if account is a valid candidate."""
    if str(account.get("platform") or "").lower() != "grok":
        return "platform_not_grok"
    if str(account.get("type") or "").lower() != "oauth":
        return "type_not_oauth"
    if str(account.get("status") or "").lower() != "active":
        return "status_not_active"
    if account.get("schedulable") is not True:
        return "not_schedulable"
    if not in_group(account, group_id):
        return "wrong_group"
    if not is_parent_empty(account.get("parent_account_id")):
        return "parent_not_empty"
    if not is_proxy_empty(account.get("proxy_id")):
        return "proxy_not_empty"
    if snapshot_status_code(account) != 429:
        return "snapshot_not_429"
    if not rate_limit_reset_present(account):
        return "rate_limit_reset_missing"
    return None


def is_candidate(account: Mapping[str, Any], group_id: int) -> bool:
    return candidate_reason(account, group_id) is None


def already_applied(account: Mapping[str, Any], expected_proxy_id: int, group_id: int) -> bool:
    """Idempotent resume: already bound to expected proxy with healthy flags."""
    if as_int(account.get("id")) is None:
        return False
    if str(account.get("platform") or "").lower() != "grok":
        return False
    if str(account.get("type") or "").lower() != "oauth":
        return False
    if str(account.get("status") or "").lower() != "active":
        return False
    if account.get("schedulable") is not True:
        return False
    if as_int(account.get("proxy_id")) != expected_proxy_id:
        return False
    if not in_group(account, group_id):
        return False
    if not is_parent_empty(account.get("parent_account_id")):
        return False
    return True


# ---------------------------------------------------------------------------
# Balancing
# ---------------------------------------------------------------------------


def compute_balanced_quotas(
    proxy_ids: Sequence[int],
    current_counts: Mapping[int, int],
    candidate_count: int,
) -> dict[int, int]:
    """Compute how many new candidates each proxy should receive.

    Final totals across the proxy pool are made as equal as possible by only
    adding bindings (never unbinding existing ones).
    """
    if not proxy_ids:
        raise ToolError("proxy pool is empty")
    if candidate_count < 0:
        raise ToolError("candidate_count must be >= 0")

    proxies = sorted({int(pid) for pid in proxy_ids})
    current = {pid: int(current_counts.get(pid, 0)) for pid in proxies}
    for pid, count in current.items():
        if count < 0:
            raise ToolError(f"negative current count for proxy {pid}")

    # Greedy water-fill: repeatedly give one unit to the lowest final total.
    finals = dict(current)
    for _ in range(candidate_count):
        target = min(proxies, key=lambda pid: (finals[pid], pid))
        finals[target] += 1

    quotas = {pid: finals[pid] - current[pid] for pid in proxies}
    if sum(quotas.values()) != candidate_count:
        raise ToolError("internal quota balance error")
    return quotas


def assign_candidates_to_proxies(
    candidate_ids: Sequence[int],
    quotas: Mapping[int, int],
    seed: int,
) -> list[dict[str, int]]:
    """Random candidate-to-proxy mapping within quotas; reproducible by seed."""
    ids = [int(x) for x in candidate_ids]
    slots: list[int] = []
    for pid in sorted(quotas):
        q = int(quotas[pid])
        if q < 0:
            raise ToolError(f"negative quota for proxy {pid}")
        slots.extend([int(pid)] * q)
    if len(slots) != len(ids):
        raise ToolError(
            f"quota total {len(slots)} does not match candidate count {len(ids)}"
        )
    rng = random.Random(seed)
    # Shuffle both sides with independent streams derived from the same seed
    # for stable reproducibility while avoiding position bias.
    shuffled_ids = list(ids)
    shuffled_slots = list(slots)
    rng.shuffle(shuffled_ids)
    rng.shuffle(shuffled_slots)
    mapping = [
        {"account_id": account_id, "proxy_id": proxy_id}
        for account_id, proxy_id in zip(shuffled_ids, shuffled_slots)
    ]
    mapping.sort(key=lambda item: (item["account_id"], item["proxy_id"]))
    return mapping


# ---------------------------------------------------------------------------
# Admin API client
# ---------------------------------------------------------------------------


HttpDo = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, dict[str, Any]]]


def default_http_do(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            status = int(getattr(response, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        # Read and discard body; never surface it.
        try:
            exc.read()
        except Exception:
            pass
        raise ToolError(f"Admin API HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise ToolError("Admin API network error") from None
    except TimeoutError:
        raise ToolError("Admin API timeout") from None

    if not raw:
        return status, {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ToolError("Admin API returned non-JSON response") from None
    if not isinstance(payload, dict):
        raise ToolError("Admin API returned non-object JSON")
    return status, payload


class AdminClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        http_do: HttpDo | None = None,
    ) -> None:
        base = base_url.strip().rstrip("/")
        if not base:
            raise ToolError("base URL is required")
        if not api_key.strip():
            raise ToolError("admin API key is required")
        self.base_url = base
        self._api_key = api_key.strip()
        self.timeout = timeout
        self.http_do = http_do or default_http_do
        self.calls: list[dict[str, Any]] = []

    def _headers(self, has_body: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "x-api-key": self._api_key,
        }
        if has_body:
            headers["Content-Type"] = "application/json"
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        method = method.upper()
        if not path.startswith("/"):
            path = "/" + path
        # Hard ban dangerous endpoints.
        lowered = path.lower()
        if lowered.endswith("/test") or "/test?" in lowered:
            raise ToolError("refusing to call /test endpoint")
        if method == "DELETE":
            raise ToolError("refusing DELETE method")
        if "clear-rate-limit" in lowered or "clear_rate_limit" in lowered:
            raise ToolError("refusing clear-rate-limit endpoint")
        if "temp-unschedulable" in lowered and method in {"DELETE", "POST", "PUT"}:
            raise ToolError("refusing temp-unschedulable mutation")
        if "clear-error" in lowered:
            raise ToolError("refusing clear-error endpoint")

        url = self.base_url + path
        if query:
            pairs = []
            for key in sorted(query):
                value = query[key]
                if value is None:
                    continue
                pairs.append((key, str(value)))
            if pairs:
                url = url + "?" + urllib.parse.urlencode(pairs)

        raw_body = None if body is None else canonical_json(body)
        self.calls.append(
            {
                "method": method,
                "path": path,
                "query": dict(query or {}),
                "body": dict(body) if isinstance(body, Mapping) else body,
            }
        )
        try:
            status, payload = self.http_do(
                method,
                url,
                self._headers(raw_body is not None),
                raw_body,
                self.timeout,
            )
        except ToolError:
            raise
        except urllib.error.HTTPError as exc:
            try:
                exc.read()
            except Exception:
                pass
            raise ToolError(f"Admin API HTTP {exc.code}") from None
        except urllib.error.URLError:
            raise ToolError("Admin API network error") from None
        except Exception as exc:
            raise ToolError(safe_error_message(exc)) from None
        if status >= 400:
            raise ToolError(f"Admin API HTTP {status}")
        code = payload.get("code")
        if code not in (0, "0", None):
            # Do not echo message/body.
            raise ToolError(f"Admin API error code {code!r}")
        return payload.get("data", payload)

    def get_account(self, account_id: int) -> dict[str, Any]:
        data = self.request("GET", f"/api/v1/admin/accounts/{int(account_id)}")
        if not isinstance(data, dict):
            raise ToolError("account response is not an object")
        return data

    def set_schedulable(self, account_id: int, schedulable: bool) -> dict[str, Any]:
        data = self.request(
            "POST",
            f"/api/v1/admin/accounts/{int(account_id)}/schedulable",
            body={"schedulable": bool(schedulable)},
        )
        return data if isinstance(data, dict) else {}

    def set_proxy_id(self, account_id: int, proxy_id: int) -> dict[str, Any]:
        data = self.request(
            "PUT",
            f"/api/v1/admin/accounts/{int(account_id)}",
            body={"proxy_id": int(proxy_id)},
        )
        return data if isinstance(data, dict) else {}

    def iter_accounts(
        self,
        *,
        platform: str,
        account_type: str,
        group_id: int,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> list[dict[str, Any]]:
        if page_size < 1 or page_size > 1000:
            raise ToolError("page_size must be between 1 and 1000")
        page = 1
        total: int | None = None
        items: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        while True:
            data = self.request(
                "GET",
                "/api/v1/admin/accounts",
                query={
                    "page": page,
                    "page_size": page_size,
                    "platform": platform,
                    "type": account_type,
                    "group": group_id,
                    "sort_by": "id",
                    "sort_order": "asc",
                },
            )
            if not isinstance(data, dict):
                raise ToolError("accounts list data is not an object")
            batch = data.get("items")
            if not isinstance(batch, list):
                raise ToolError("accounts list missing items")
            if total is None:
                total = as_int(data.get("total"))
                if total is None:
                    total = len(batch)
            for item in batch:
                if not isinstance(item, dict):
                    raise ToolError("account item is not an object")
                account_id = as_int(item.get("id"))
                if account_id is None:
                    raise ToolError("account item missing id")
                if account_id in seen_ids:
                    raise ToolError("accounts pagination returned duplicate ids")
                seen_ids.add(account_id)
                items.append(item)
            if not batch:
                break
            if total is not None and len(items) >= total:
                break
            page += 1
            if page > 100000:
                raise ToolError("accounts pagination guard exceeded")
        return items

    def verify_proxies_active(self, proxy_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        wanted = sorted({int(pid) for pid in proxy_ids})
        if not wanted:
            raise ToolError("proxy ids are required")
        found: dict[int, dict[str, Any]] = {}
        # Prefer /all for simplicity; fall back to paginated list.
        try:
            data = self.request("GET", "/api/v1/admin/proxies/all")
            rows: list[Any]
            if isinstance(data, list):
                rows = data
            elif isinstance(data, dict) and isinstance(data.get("items"), list):
                rows = data["items"]
            else:
                rows = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                pid = as_int(row.get("id"))
                if pid in wanted:
                    found[pid] = row
        except ToolError:
            found = {}

        if len(found) < len(wanted):
            # Paginate.
            page = 1
            while len(found) < len(wanted) and page <= 10000:
                data = self.request(
                    "GET",
                    "/api/v1/admin/proxies",
                    query={
                        "page": page,
                        "page_size": 100,
                        "status": "active",
                        "sort_by": "id",
                        "sort_order": "asc",
                    },
                )
                if not isinstance(data, dict):
                    raise ToolError("proxies list data is not an object")
                batch = data.get("items") or []
                if not isinstance(batch, list) or not batch:
                    break
                for row in batch:
                    if not isinstance(row, dict):
                        continue
                    pid = as_int(row.get("id"))
                    if pid in wanted:
                        found[pid] = row
                page += 1

        missing = [pid for pid in wanted if pid not in found]
        if missing:
            raise ToolError(f"proxy ids not found: {missing}")

        inactive = [
            pid
            for pid, row in found.items()
            if str(row.get("status") or "").lower() != "active"
        ]
        if inactive:
            raise ToolError(f"proxy ids not active: {sorted(inactive)}")

        # Return only non-secret fields.
        safe: dict[int, dict[str, Any]] = {}
        for pid, row in found.items():
            safe[pid] = {
                "id": pid,
                "name": row.get("name"),
                "status": row.get("status"),
                "protocol": row.get("protocol"),
            }
        return safe


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


RunCommand = Callable[..., subprocess.CompletedProcess[bytes]]


def create_postgres_backup(
    *,
    backup_dir: Path,
    postgres_container: str,
    pg_user: str,
    pg_db: str,
    run_command: RunCommand | None = None,
) -> dict[str, Any]:
    if not postgres_container.strip():
        raise ToolError("postgres container is required")
    if not pg_user.strip() or not pg_db.strip():
        raise ToolError("pg user and db are required")
    ensure_dir_0700(backup_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = backup_dir / f"pre-bind-quota-egress-{stamp}.dump"
    runner = run_command or subprocess.run
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(backup_dir))
    tmp_path = Path(tmp_name)
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "wb") as handle:
            proc = runner(
                [
                    "docker",
                    "exec",
                    postgres_container,
                    "pg_dump",
                    "-U",
                    pg_user,
                    "-d",
                    pg_db,
                    "-Fc",
                    "--no-owner",
                    "--no-acl",
                ],
                stdout=handle,
                stderr=subprocess.PIPE,
                check=False,
            )
            handle.flush()
            os.fsync(handle.fileno())
        if getattr(proc, "returncode", 1) != 0:
            raise ToolError("pg_dump backup failed")

        with tmp_path.open("rb") as handle:
            verify = runner(
                [
                    "docker",
                    "exec",
                    "-i",
                    postgres_container,
                    "pg_restore",
                    "-l",
                ],
                stdin=handle,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
            )
        if getattr(verify, "returncode", 1) != 0:
            raise ToolError("pg_restore -l verification failed")

        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
        dir_fd = os.open(str(backup_dir), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    digest = sha256_file(path)
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": digest,
        "pg_restore_list_verified": True,
        "created_at": utc_now(),
        "postgres_container": postgres_container,
        "pg_db": pg_db,
        "pg_user": pg_user,
        "format": "custom",
        "flags": ["-Fc", "--no-owner", "--no-acl"],
    }


# ---------------------------------------------------------------------------
# Plan / Apply
# ---------------------------------------------------------------------------


def build_plan(
    *,
    client: AdminClient,
    group_id: int,
    proxy_ids: Sequence[int],
    seed: int,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    proxies = client.verify_proxies_active(proxy_ids)
    pool = sorted(proxies)
    accounts = client.iter_accounts(
        platform="grok",
        account_type="oauth",
        group_id=group_id,
        page_size=page_size,
    )

    current_counts = {pid: 0 for pid in pool}
    rejected: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []

    for account in accounts:
        account_id = as_int(account.get("id"))
        if account_id is None:
            continue
        # Count existing pool bindings for all group accounts (any status).
        pid = as_int(account.get("proxy_id"))
        if pid in current_counts:
            current_counts[pid] += 1

        reason = candidate_reason(account, group_id)
        if reason is None:
            candidates.append(account)
        else:
            # Only record rejections that were almost-candidates or notable.
            if reason in {
                "proxy_not_empty",
                "snapshot_not_429",
                "rate_limit_reset_missing",
                "not_schedulable",
                "status_not_active",
                "parent_not_empty",
                "wrong_group",
            }:
                rejected.append({"account_id": account_id, "reason": reason})

    candidates.sort(key=lambda row: int(as_int(row.get("id")) or 0))
    candidate_ids = [int(as_int(row.get("id")) or 0) for row in candidates]
    quotas = compute_balanced_quotas(pool, current_counts, len(candidate_ids))
    assignments = assign_candidates_to_proxies(candidate_ids, quotas, seed)
    by_id = {int(as_int(row.get("id")) or 0): row for row in candidates}

    assignment_rows: list[dict[str, Any]] = []
    for item in assignments:
        account = by_id[item["account_id"]]
        assignment_rows.append(
            {
                "account_id": item["account_id"],
                "proxy_id": item["proxy_id"],
                "before": non_secret_before(account),
            }
        )

    final_totals = {
        str(pid): int(current_counts[pid]) + int(quotas[pid]) for pid in pool
    }
    plan = {
        "tool": TOOL_NAME,
        "version": PLAN_VERSION,
        "created_at": utc_now(),
        "base_url_origin": normalized_url_origin(client.base_url),
        "group_id": int(group_id),
        "platform": "grok",
        "type": "oauth",
        "seed": int(seed),
        "proxy_pool": [
            {"id": pid, "name": proxies[pid].get("name"), "status": proxies[pid].get("status")}
            for pid in pool
        ],
        "current_proxy_counts": {str(pid): current_counts[pid] for pid in pool},
        "quotas": {str(pid): quotas[pid] for pid in pool},
        "final_proxy_totals": final_totals,
        "candidate_count": len(assignment_rows),
        "assignments": assignment_rows,
        "rejected_sample": rejected[:200],
        "rejected_count": len(rejected),
        "notes": [
            "Outcome proves binding_applied only; does not claim quota recovered.",
            "snapshot/cooldown fields are recorded as-is and must not be cleared by apply.",
        ],
    }
    plan["plan_sha256"] = sha256_json({k: v for k, v in plan.items() if k != "plan_sha256"})
    return plan


def write_plan_artifacts(plan: dict[str, Any], output_dir: Path) -> dict[str, str]:
    ensure_dir_0700(output_dir)
    plan_path = output_dir / "plan.json"
    sha_path = output_dir / "plan.sha256"
    body = {k: v for k, v in plan.items() if k != "plan_sha256"}
    digest = sha256_json(body)
    plan_out = dict(plan)
    plan_out["plan_sha256"] = digest
    atomic_write_json(plan_path, plan_out, mode=0o600)
    atomic_write_bytes(sha_path, (digest + "\n").encode("utf-8"), mode=0o600)
    if file_mode(plan_path) != 0o600 or file_mode(sha_path) != 0o600:
        raise ToolError("plan artifacts must be mode 0600")
    if file_mode(output_dir) != 0o700:
        raise ToolError("plan output directory must be mode 0700")
    return {"plan_path": str(plan_path), "sha256_path": str(sha_path), "plan_sha256": digest}


def load_plan(plan_path: Path) -> dict[str, Any]:
    try:
        raw = plan_path.read_text(encoding="utf-8")
        plan = json.loads(raw)
    except FileNotFoundError as exc:
        raise ToolError(f"plan not found: {plan_path}") from exc
    except json.JSONDecodeError as exc:
        raise ToolError("plan JSON is invalid") from exc
    if not isinstance(plan, dict):
        raise ToolError("plan must be a JSON object")
    return plan


def verify_plan_sha256(plan: Mapping[str, Any], plan_path: Path | None = None) -> str:
    stored = str(plan.get("plan_sha256") or "").strip().lower()
    body = {k: v for k, v in plan.items() if k != "plan_sha256"}
    digest = sha256_json(body)
    if len(stored) != 64 or stored != digest:
        raise ToolError("plan SHA256 mismatch")
    if plan_path is not None:
        if not plan_path.is_file() or file_mode(plan_path) != 0o600:
            raise ToolError("plan file must exist with mode 0600")
        sha_file = plan_path.with_name("plan.sha256")
        if not sha_file.is_file() or file_mode(sha_file) != 0o600:
            raise ToolError("plan.sha256 must exist with mode 0600")
        file_digest = sha_file.read_text(encoding="utf-8").strip().lower()
        if file_digest != digest:
            raise ToolError("plan.sha256 file mismatch")
    return digest


def validate_plan_for_apply(
    plan: Mapping[str, Any], client: AdminClient
) -> tuple[int, list[Mapping[str, Any]], list[int]]:
    if plan.get("tool") != TOOL_NAME:
        raise ToolError("plan tool identity mismatch")
    if as_int(plan.get("version")) != PLAN_VERSION:
        raise ToolError("plan version mismatch")
    if str(plan.get("platform") or "").lower() != "grok":
        raise ToolError("plan platform mismatch")
    if str(plan.get("type") or "").lower() != "oauth":
        raise ToolError("plan account type mismatch")
    if str(plan.get("base_url_origin") or "").strip().lower() != normalized_url_origin(
        client.base_url
    ):
        raise ToolError("apply base URL origin does not match plan")

    group_id = as_int(plan.get("group_id"))
    if group_id is None or group_id < 1:
        raise ToolError("plan missing valid group_id")

    raw_pool = plan.get("proxy_pool")
    if not isinstance(raw_pool, list) or not raw_pool:
        raise ToolError("plan missing proxy_pool")
    proxy_ids: list[int] = []
    for row in raw_pool:
        if not isinstance(row, Mapping):
            raise ToolError("plan proxy_pool row must be an object")
        proxy_id = as_int(row.get("id"))
        if proxy_id is None or proxy_id < 1:
            raise ToolError("plan proxy_pool contains invalid id")
        proxy_ids.append(proxy_id)
    if len(set(proxy_ids)) != len(proxy_ids):
        raise ToolError("plan proxy_pool contains duplicate ids")

    raw_assignments = plan.get("assignments")
    if not isinstance(raw_assignments, list):
        raise ToolError("plan missing assignments")
    candidate_count = as_int(plan.get("candidate_count"))
    if candidate_count is None or candidate_count != len(raw_assignments):
        raise ToolError("plan candidate_count does not match assignments")

    assignment_ids: set[int] = set()
    assignments: list[Mapping[str, Any]] = []
    for row in raw_assignments:
        if not isinstance(row, Mapping):
            raise ToolError("assignment row must be an object")
        account_id = as_int(row.get("account_id"))
        proxy_id = as_int(row.get("proxy_id"))
        before = row.get("before")
        if account_id is None or account_id < 1 or proxy_id is None:
            raise ToolError("assignment missing valid account_id/proxy_id")
        if account_id in assignment_ids:
            raise ToolError("plan contains duplicate assignment account ids")
        if proxy_id not in proxy_ids:
            raise ToolError("assignment proxy is outside plan proxy_pool")
        if not isinstance(before, Mapping):
            raise ToolError("assignment missing before snapshot")
        if as_int(before.get("id")) != account_id:
            raise ToolError("assignment before id mismatch")
        assignment_ids.add(account_id)
        assignments.append(row)

    client.verify_proxies_active(proxy_ids)
    return group_id, assignments, proxy_ids


def frozen_state_matches(
    planned_before: Mapping[str, Any],
    live: Mapping[str, Any],
    *,
    expected_proxy_id: int,
    allow_already_applied: bool,
) -> bool:
    expected = frozen_before_fields(planned_before)
    actual = frozen_before_fields(live)
    if allow_already_applied:
        if expected["proxy_id"] != 0 or actual["proxy_id"] != expected_proxy_id:
            return False
        expected = dict(expected)
        expected["proxy_id"] = expected_proxy_id
    return expected == actual


def apply_one_account(
    client: AdminClient,
    *,
    account_id: int,
    expected_proxy_id: int,
    group_id: int,
    before: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    live = client.get_account(account_id)
    is_applied = already_applied(live, expected_proxy_id, group_id)
    if before is not None and not frozen_state_matches(
        before,
        live,
        expected_proxy_id=expected_proxy_id,
        allow_already_applied=is_applied,
    ):
        raise ToolError(f"account {account_id} frozen state drifted before mutation")
    if is_applied:
        return {
            "account_id": account_id,
            "status": "already_applied",
            "proxy_id": expected_proxy_id,
            "planned_before": before or non_secret_before(live),
            "live_before": non_secret_before(live),
            "after": non_secret_before(live),
            "outcome": "binding_applied",
        }

    reason = candidate_reason(live, group_id)
    if reason is not None:
        raise ToolError(f"account {account_id} no longer a candidate: {reason}")

    original_schedulable = bool(live.get("schedulable"))
    original_proxy = as_int(live.get("proxy_id"))
    rollback_proxy = 0 if is_proxy_empty(original_proxy) else int(original_proxy or 0)

    live_before = non_secret_before(live)
    preserved_before = preserved_status_state(live)
    steps: list[str] = []
    try:
        client.set_schedulable(account_id, False)
        steps.append("schedulable_false")
        client.set_proxy_id(account_id, expected_proxy_id)
        steps.append("proxy_set")
        client.set_schedulable(account_id, True)
        steps.append("schedulable_true")
        after = client.get_account(account_id)
        steps.append("verify_get")
        if as_int(after.get("proxy_id")) != expected_proxy_id:
            raise ToolError(f"account {account_id} proxy_id verify failed")
        if after.get("schedulable") is not True:
            raise ToolError(f"account {account_id} schedulable verify failed")
        if str(after.get("status") or "").lower() != "active":
            raise ToolError(f"account {account_id} status verify failed")
        if not in_group(after, group_id):
            raise ToolError(f"account {account_id} group verify failed")
        if not is_parent_empty(after.get("parent_account_id")):
            raise ToolError(f"account {account_id} parent verify failed")
        if preserved_status_state(after) != preserved_before:
            raise ToolError(f"account {account_id} snapshot/cooldown state changed")
        return {
            "account_id": account_id,
            "status": "applied",
            "proxy_id": expected_proxy_id,
            "planned_before": before or live_before,
            "live_before": live_before,
            "after": non_secret_before(after),
            "steps": steps,
            "outcome": "binding_applied",
        }
    except Exception as exc:
        # Best-effort rollback of current account only.
        rollback: dict[str, Any] = {"attempted": True, "proxy_id": rollback_proxy}
        try:
            client.set_proxy_id(account_id, rollback_proxy)
            rollback["proxy_restored"] = True
        except Exception as rex:
            rollback["proxy_restored"] = False
            rollback["proxy_error"] = safe_error_message(rex)
        try:
            client.set_schedulable(account_id, original_schedulable)
            rollback["schedulable_restored"] = True
            rollback["schedulable"] = original_schedulable
        except Exception as rex:
            rollback["schedulable_restored"] = False
            rollback["schedulable_error"] = safe_error_message(rex)
        raise ToolError(
            f"account {account_id} apply failed: {safe_error_message(exc)}; rollback={rollback}"
        ) from None


def apply_plan(
    *,
    client: AdminClient,
    plan: Mapping[str, Any],
    plan_path: Path,
    confirm_production_write: bool,
    backup_dir: Path,
    postgres_container: str,
    pg_user: str,
    pg_db: str,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    run_command: RunCommand | None = None,
    result_dir: Path | None = None,
) -> dict[str, Any]:
    if not confirm_production_write:
        raise ToolError("apply requires --confirm-production-write")
    if failure_limit < 0:
        raise ToolError("failure_limit must be >= 0")

    digest = verify_plan_sha256(plan, plan_path)
    group_id, assignments, _proxy_ids = validate_plan_for_apply(plan, client)

    out_dir = result_dir or plan_path.parent
    ensure_dir_0700(out_dir)
    results_path = out_dir / "apply-results.jsonl"
    summary_path = out_dir / "apply-summary.json"

    # Pre-validate all candidates still match (or already_applied) BEFORE backup writes.
    # Backup is still required before any mutating API writes.
    prepared: list[dict[str, Any]] = []
    for row in assignments:
        account_id = as_int(row.get("account_id"))
        proxy_id = as_int(row.get("proxy_id"))
        if account_id is None or proxy_id is None:  # guarded by plan validation
            raise ToolError("assignment missing account_id/proxy_id")
        planned_before = row.get("before")
        if not isinstance(planned_before, Mapping):  # guarded by plan validation
            raise ToolError("assignment missing before snapshot")
        live = client.get_account(account_id)
        is_applied = already_applied(live, proxy_id, group_id)
        if not frozen_state_matches(
            planned_before,
            live,
            expected_proxy_id=proxy_id,
            allow_already_applied=is_applied,
        ):
            raise ToolError(f"precheck frozen state drift for account {account_id}")
        if is_applied:
            prepared.append(
                {
                    "account_id": account_id,
                    "proxy_id": proxy_id,
                    "before": planned_before,
                    "live_before": non_secret_before(live),
                    "skip": "already_applied",
                }
            )
            continue
        reason = candidate_reason(live, group_id)
        if reason is not None:
            raise ToolError(
                f"precheck failed for account {account_id}: {reason}"
            )
        prepared.append(
            {
                "account_id": account_id,
                "proxy_id": proxy_id,
                "before": planned_before,
                "live_before": non_secret_before(live),
                "skip": None,
            }
        )

    mutating = [row for row in prepared if not row.get("skip")]
    backup_meta: dict[str, Any] | None = None
    if mutating:
        backup_meta = create_postgres_backup(
            backup_dir=backup_dir,
            postgres_container=postgres_container,
            pg_user=pg_user,
            pg_db=pg_db,
            run_command=run_command,
        )
        atomic_write_json(out_dir / "backup.json", backup_meta, mode=0o600)

    results: list[dict[str, Any]] = []
    failures = 0
    for row in prepared:
        account_id = int(row["account_id"])
        proxy_id = int(row["proxy_id"])
        if row.get("skip") == "already_applied":
            item = {
                "account_id": account_id,
                "status": "already_applied",
                "proxy_id": proxy_id,
                "planned_before": row.get("before"),
                "live_before": row.get("live_before"),
                "outcome": "binding_applied",
                "at": utc_now(),
            }
            results.append(item)
            append_jsonl(results_path, item)
            continue
        try:
            item = apply_one_account(
                client,
                account_id=account_id,
                expected_proxy_id=proxy_id,
                group_id=group_id,
                before=row.get("before"),
            )
            item["at"] = utc_now()
            results.append(item)
            append_jsonl(results_path, item)
        except Exception as exc:
            failures += 1
            item = {
                "account_id": account_id,
                "status": "failed",
                "proxy_id": proxy_id,
                "error": safe_error_message(exc),
                "outcome": "binding_failed",
                "at": utc_now(),
            }
            results.append(item)
            append_jsonl(results_path, item)
            if failure_limit > 0 and failures >= failure_limit:
                break

    failed_count = sum(1 for result in results if result.get("status") == "failed")
    applied_count = sum(1 for result in results if result.get("status") == "applied")
    already_count = sum(
        1 for result in results if result.get("status") == "already_applied"
    )
    if failed_count:
        outcome = "binding_partial_failure" if applied_count or already_count else "binding_failed"
    elif prepared:
        outcome = "binding_applied"
    else:
        outcome = "no_candidates"
    summary = {
        "tool": TOOL_NAME,
        "plan_sha256": digest,
        "group_id": group_id,
        "backup": backup_meta,
        "total": len(prepared),
        "applied": applied_count,
        "already_applied": already_count,
        "failed": failed_count,
        "stopped_early": failure_limit > 0 and failures >= failure_limit,
        "outcome": outcome,
        "quota_recovered_claimed": False,
        "finished_at": utc_now(),
        "results_path": str(results_path),
    }
    atomic_write_json(summary_path, summary, mode=0o600)
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_proxy_ids(value: str) -> list[int]:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("proxy ids required")
    ids: list[int] = []
    for part in parts:
        try:
            num = int(part)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid proxy id: {part}") from exc
        if num < 1:
            raise argparse.ArgumentTypeError(f"proxy id must be >= 1: {part}")
        ids.append(num)
    # unique preserve order
    seen: set[int] = set()
    out: list[int] = []
    for num in ids:
        if num not in seen:
            seen.add(num)
            out.append(num)
    return out


def read_admin_key_file(path: Path) -> str:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ToolError("admin key file not found") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ToolError("admin key file must be a regular non-symlink file")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ToolError("admin key file mode must be 0600")
    if metadata.st_uid != os.geteuid():
        raise ToolError("admin key file must be owned by the current user")
    if metadata.st_size > 64 * 1024:
        raise ToolError("admin key file is unexpectedly large")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ToolError("cannot read admin key file") from exc
    if not value or "\n" in value or "\r" in value:
        raise ToolError("admin key file must contain exactly one non-empty line")
    return value


def resolve_api_key(key_file: Path | None) -> str:
    if key_file is not None:
        return read_admin_key_file(Path(key_file))
    for env_name in ("SUB2API_ADMIN_API_KEY_FILE", "SUB2API_ADMIN_KEY_FILE"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return read_admin_key_file(Path(value))
    for env_name in ("SUB2API_ADMIN_API_KEY", "SUB2API_ADMIN_KEY"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    raise ToolError(
        "admin API key required via --admin-key-file (preferred) or compatibility environment"
    )


def resolve_base_url(cli_value: str | None) -> str:
    if cli_value and cli_value.strip():
        return cli_value.strip().rstrip("/")
    for env_name in ("SUB2API_BASE_URL", "SUB2API_URL", "SUB2API_BASE"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value.rstrip("/")
    raise ToolError("base URL required via --base-url or SUB2API_BASE_URL")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="Build a reproducible binding plan")
    plan.add_argument("--base-url", default=None)
    plan.add_argument(
        "--admin-key-file",
        type=Path,
        default=None,
        help="mode-0600 file containing the Admin API key",
    )
    plan.add_argument("--group-id", type=int, required=True)
    plan.add_argument("--proxy-ids", type=parse_proxy_ids, required=True)
    plan.add_argument("--output-dir", type=Path, required=True)
    plan.add_argument("--seed", type=int, default=None, help="RNG seed (default: random)")
    plan.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    plan.add_argument("--timeout", type=float, default=30.0)

    apply = sub.add_parser("apply", help="Apply a plan with backup and per-account rollback")
    apply.add_argument("--plan", type=Path, required=True, dest="plan_path")
    apply.add_argument("--base-url", default=None)
    apply.add_argument(
        "--admin-key-file",
        type=Path,
        default=None,
        help="mode-0600 file containing the Admin API key",
    )
    apply.add_argument("--confirm-production-write", action="store_true")
    apply.add_argument("--backup-dir", type=Path, required=True)
    apply.add_argument("--postgres-container", required=True)
    apply.add_argument("--pg-user", required=True)
    apply.add_argument("--pg-db", required=True)
    apply.add_argument("--failure-limit", type=int, default=DEFAULT_FAILURE_LIMIT)
    apply.add_argument("--timeout", type=float, default=30.0)
    apply.add_argument("--result-dir", type=Path, default=None)

    return parser


def cmd_plan(args: argparse.Namespace) -> int:
    seed = args.seed if args.seed is not None else random.SystemRandom().randint(1, 2**31 - 1)
    client = AdminClient(
        resolve_base_url(args.base_url),
        resolve_api_key(args.admin_key_file),
        timeout=float(args.timeout),
    )
    plan = build_plan(
        client=client,
        group_id=int(args.group_id),
        proxy_ids=list(args.proxy_ids),
        seed=int(seed),
        page_size=int(args.page_size),
    )
    artifacts = write_plan_artifacts(plan, Path(args.output_dir))
    summary = {
        "status": "planned",
        "candidate_count": plan["candidate_count"],
        "seed": plan["seed"],
        "quotas": plan["quotas"],
        "plan_sha256": artifacts["plan_sha256"],
        "plan_path": artifacts["plan_path"],
        "outcome_claim": "binding_plan_only",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    client = AdminClient(
        resolve_base_url(args.base_url),
        resolve_api_key(args.admin_key_file),
        timeout=float(args.timeout),
    )
    plan_path = Path(args.plan_path)
    plan = load_plan(plan_path)
    summary = apply_plan(
        client=client,
        plan=plan,
        plan_path=plan_path,
        confirm_production_write=bool(args.confirm_production_write),
        backup_dir=Path(args.backup_dir),
        postgres_container=str(args.postgres_container),
        pg_user=str(args.pg_user),
        pg_db=str(args.pg_db),
        failure_limit=int(args.failure_limit),
        result_dir=Path(args.result_dir) if args.result_dir else None,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary.get("failed", 0) == 0 else 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "plan":
            return cmd_plan(args)
        if args.command == "apply":
            return cmd_apply(args)
        raise ToolError(f"unknown command: {args.command}")
    except ToolError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            json.dumps(
                {"status": "error", "error": safe_error_message(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
