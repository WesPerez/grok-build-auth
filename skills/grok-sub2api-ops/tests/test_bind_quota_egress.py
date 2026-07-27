#!/usr/bin/env python3
"""Unit tests for bind_quota_egress.py (stdlib unittest + fakes; no production access)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock


SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "bind_quota_egress.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("bind_quota_egress", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = previous
    return mod


M = load_module()


def account(
    account_id: int,
    *,
    status: str = "active",
    schedulable: bool = True,
    group_id: int = 7,
    proxy_id: int | None = None,
    parent_account_id: int | None = None,
    snapshot_status: int | None = 429,
    rate_limit_reset_at: str | None = "2026-07-28T00:00:00Z",
    platform: str = "grok",
    account_type: str = "oauth",
    name: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snap_extra = dict(extra or {})
    if snapshot_status is not None:
        snap_extra.setdefault(
            "grok_usage_snapshot",
            {"status_code": snapshot_status, "queried_at": "2026-07-27T12:00:00Z"},
        )
    return {
        "id": account_id,
        "name": name or f"acct-{account_id}",
        "platform": platform,
        "type": account_type,
        "status": status,
        "schedulable": schedulable,
        "proxy_id": proxy_id,
        "parent_account_id": parent_account_id,
        "group_ids": [group_id],
        "rate_limit_reset_at": rate_limit_reset_at,
        "updated_at": "2026-07-27T12:00:00Z",
        "extra": snap_extra,
    }


class FakeAdminState:
    def __init__(self, accounts: list[dict[str, Any]], proxies: list[dict[str, Any]]) -> None:
        self.accounts = {int(a["id"]): dict(a) for a in accounts}
        self.proxies = {int(p["id"]): dict(p) for p in proxies}
        self.calls: list[dict[str, Any]] = []
        self.fail_proxy_for: set[int] = set()
        self.fail_sched_for: set[int] = set()
        self.mutate_after_enable: dict[int, Any] = {}

    def http_do(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        # Ensure secrets never need to be in assertions via body echo.
        assert "x-api-key" in headers
        path = url.split("?", 1)[0]
        # Strip scheme/host
        idx = path.find("/api/")
        path = path[idx:] if idx >= 0 else path
        query = {}
        if "?" in url:
            from urllib.parse import parse_qs, urlparse

            query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}

        payload = json.loads(body.decode("utf-8")) if body else None
        self.calls.append({"method": method, "path": path, "query": query, "body": payload})

        if path == "/api/v1/admin/proxies/all" and method == "GET":
            return 200, {"code": 0, "data": list(self.proxies.values())}

        if path == "/api/v1/admin/proxies" and method == "GET":
            items = list(self.proxies.values())
            return 200, {
                "code": 0,
                "data": {
                    "items": items,
                    "total": len(items),
                    "page": 1,
                    "page_size": 100,
                    "pages": 1,
                },
            }

        if path == "/api/v1/admin/accounts" and method == "GET":
            group = int(query.get("group", "0") or 0)
            platform = query.get("platform", "")
            account_type = query.get("type", "")
            page = int(query.get("page", "1") or 1)
            page_size = int(query.get("page_size", "100") or 100)
            rows = [
                a
                for a in self.accounts.values()
                if a.get("platform") == platform
                and a.get("type") == account_type
                and group in (a.get("group_ids") or [])
            ]
            rows.sort(key=lambda r: int(r["id"]))
            start = (page - 1) * page_size
            batch = rows[start : start + page_size]
            return 200, {
                "code": 0,
                "data": {
                    "items": batch,
                    "total": len(rows),
                    "page": page,
                    "page_size": page_size,
                    "pages": max(1, (len(rows) + page_size - 1) // page_size),
                },
            }

        if path.startswith("/api/v1/admin/accounts/") and method == "GET":
            account_id = int(path.rsplit("/", 1)[-1])
            if account_id not in self.accounts:
                return 404, {"code": 404, "message": "missing"}
            return 200, {"code": 0, "data": dict(self.accounts[account_id])}

        if path.endswith("/schedulable") and method == "POST":
            account_id = int(path.split("/")[-2])
            if account_id in self.fail_sched_for:
                return 500, {"code": 500, "message": "boom"}
            self.accounts[account_id]["schedulable"] = bool(payload["schedulable"])
            if payload["schedulable"] is True and account_id in self.mutate_after_enable:
                self.mutate_after_enable[account_id](self.accounts[account_id])
            return 200, {"code": 0, "data": dict(self.accounts[account_id])}

        if path.startswith("/api/v1/admin/accounts/") and method == "PUT":
            account_id = int(path.rsplit("/", 1)[-1])
            if account_id in self.fail_proxy_for:
                return 500, {"code": 500, "message": "proxy fail"}
            if "proxy_id" in payload:
                pid = payload["proxy_id"]
                self.accounts[account_id]["proxy_id"] = None if pid == 0 else pid
            return 200, {"code": 0, "data": dict(self.accounts[account_id])}

        return 404, {"code": 404, "message": "not found"}


class QuotaTests(unittest.TestCase):
    def test_balanced_quotas_equalize_final_totals(self) -> None:
        # current [5, 1, 0], add 4 -> finals should be [5, 3, 2] or better balance
        # water-fill: start 5,1,0 -> give to lowest: 5,1,1 -> 5,2,1 -> 5,2,2 -> 5,3,2
        quotas = M.compute_balanced_quotas([10, 20, 30], {10: 5, 20: 1, 30: 0}, 4)
        self.assertEqual(quotas, {10: 0, 20: 2, 30: 2})
        finals = {k: {10: 5, 20: 1, 30: 0}[k] + quotas[k] for k in quotas}
        self.assertEqual(sorted(finals.values()), [2, 3, 5])

    def test_assignment_reproducible_with_seed(self) -> None:
        quotas = {1: 2, 2: 1, 3: 1}
        ids = [101, 102, 103, 104]
        a = M.assign_candidates_to_proxies(ids, quotas, seed=42)
        b = M.assign_candidates_to_proxies(ids, quotas, seed=42)
        c = M.assign_candidates_to_proxies(ids, quotas, seed=99)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        # quotas respected
        from collections import Counter

        counts = Counter(item["proxy_id"] for item in a)
        self.assertEqual(dict(counts), quotas)
        self.assertEqual(sorted(item["account_id"] for item in a), sorted(ids))


class CandidateTests(unittest.TestCase):
    def test_candidate_rejects(self) -> None:
        base = account(1)
        self.assertIsNone(M.candidate_reason(base, 7))

        cases = [
            (account(1, status="error"), "status_not_active"),
            (account(1, schedulable=False), "not_schedulable"),
            (account(1, group_id=9), "wrong_group"),
            (account(1, parent_account_id=99), "parent_not_empty"),
            (account(1, proxy_id=3), "proxy_not_empty"),
            (account(1, snapshot_status=200), "snapshot_not_429"),
            (account(1, rate_limit_reset_at=None), "rate_limit_reset_missing"),
            (account(1, platform="openai"), "platform_not_grok"),
            (account(1, account_type="apikey"), "type_not_oauth"),
        ]
        for acc, expected in cases:
            self.assertEqual(M.candidate_reason(acc, 7), expected)

    def test_proxy_and_parent_empty_helpers(self) -> None:
        self.assertTrue(M.is_proxy_empty(None))
        self.assertTrue(M.is_proxy_empty(0))
        self.assertFalse(M.is_proxy_empty(5))
        self.assertTrue(M.is_parent_empty(None))
        self.assertTrue(M.is_parent_empty(0))
        self.assertFalse(M.is_parent_empty(2))


class PlanArtifactTests(unittest.TestCase):
    def test_plan_hash_and_permissions(self) -> None:
        state = FakeAdminState(
            accounts=[
                account(1),
                account(2),
                account(3, proxy_id=11),  # already bound, counts toward current
                account(4, snapshot_status=200),  # rejected
            ],
            proxies=[
                {"id": 11, "name": "p11", "status": "active", "protocol": "http"},
                {"id": 12, "name": "p12", "status": "active", "protocol": "http"},
            ],
        )
        client = M.AdminClient("http://example.local", "secret-key", http_do=state.http_do)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "plan-out"
            plan = M.build_plan(
                client=client,
                group_id=7,
                proxy_ids=[11, 12],
                seed=7,
            )
            # candidates: 1 and 2 only
            self.assertEqual(plan["candidate_count"], 2)
            self.assertEqual(plan["current_proxy_counts"], {"11": 1, "12": 0})
            self.assertNotIn("name", plan["assignments"][0]["before"])
            artifacts = M.write_plan_artifacts(plan, out)
            plan_path = Path(artifacts["plan_path"])
            sha_path = Path(artifacts["sha256_path"])
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(plan_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(sha_path.stat().st_mode), 0o600)
            loaded = json.loads(plan_path.read_text(encoding="utf-8"))
            digest = M.verify_plan_sha256(loaded, plan_path)
            self.assertEqual(digest, artifacts["plan_sha256"])
            # tamper
            loaded["seed"] = 999
            with self.assertRaises(M.ToolError):
                M.verify_plan_sha256(loaded, plan_path)


class ApplyTests(unittest.TestCase):
    def _ready_state(self) -> FakeAdminState:
        return FakeAdminState(
            accounts=[
                account(1),
                account(2),
                account(3, proxy_id=11),
            ],
            proxies=[
                {"id": 11, "name": "p11", "status": "active"},
                {"id": 12, "name": "p12", "status": "active"},
            ],
        )

    def _plan_and_client(self, state: FakeAdminState, seed: int = 1):
        client = M.AdminClient("http://example.local", "secret-key", http_do=state.http_do)
        plan = M.build_plan(client=client, group_id=7, proxy_ids=[11, 12], seed=seed)
        return client, plan

    def test_apply_requires_confirm(self) -> None:
        state = self._ready_state()
        client, plan = self._plan_and_client(state)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            artifacts = M.write_plan_artifacts(plan, tmp_path / "p")
            with self.assertRaises(M.ToolError) as ctx:
                M.apply_plan(
                    client=client,
                    plan=plan,
                    plan_path=Path(artifacts["plan_path"]),
                    confirm_production_write=False,
                    backup_dir=tmp_path / "bak",
                    postgres_container="pg",
                    pg_user="u",
                    pg_db="db",
                )
            self.assertIn("confirm-production-write", str(ctx.exception))

    def test_backup_failure_zero_api_writes(self) -> None:
        state = self._ready_state()
        client, plan = self._plan_and_client(state)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            artifacts = M.write_plan_artifacts(plan, tmp_path / "p")
            # Capture call count after plan build (plan used GETs)
            calls_after_plan = len(state.calls)

            def boom_run(*args, **kwargs):
                return SimpleNamespace(returncode=1, stderr=b"fail")

            with self.assertRaises(M.ToolError):
                M.apply_plan(
                    client=client,
                    plan=json.loads(Path(artifacts["plan_path"]).read_text(encoding="utf-8")),
                    plan_path=Path(artifacts["plan_path"]),
                    confirm_production_write=True,
                    backup_dir=tmp_path / "bak",
                    postgres_container="pg",
                    pg_user="u",
                    pg_db="db",
                    run_command=boom_run,
                )
            post = state.calls[calls_after_plan:]
            write_calls = [
                c
                for c in post
                if c["method"] in {"POST", "PUT", "PATCH", "DELETE"}
            ]
            self.assertEqual(write_calls, [], msg=f"unexpected writes: {write_calls}")
            # Only precheck GETs allowed
            self.assertTrue(all(c["method"] == "GET" for c in post))

    def test_apply_revalidates_proxy_pool_before_backup(self) -> None:
        state = self._ready_state()
        client, plan = self._plan_and_client(state)
        state.proxies[11]["status"] = "inactive"
        backup_calls: list[list[str]] = []

        def must_not_backup(cmd, **kwargs):
            backup_calls.append(cmd)
            raise AssertionError("backup must not run after proxy validation failure")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            artifacts = M.write_plan_artifacts(plan, tmp_path / "p")
            loaded = json.loads(Path(artifacts["plan_path"]).read_text(encoding="utf-8"))
            with self.assertRaises(M.ToolError):
                M.apply_plan(
                    client=client,
                    plan=loaded,
                    plan_path=Path(artifacts["plan_path"]),
                    confirm_production_write=True,
                    backup_dir=tmp_path / "bak",
                    postgres_container="pg",
                    pg_user="u",
                    pg_db="db",
                    run_command=must_not_backup,
                )
        self.assertEqual(backup_calls, [])

    def test_apply_rejects_plan_shape_and_host_mismatch(self) -> None:
        state = self._ready_state()
        client, plan = self._plan_and_client(state)
        broken = dict(plan)
        broken["candidate_count"] = int(plan["candidate_count"]) + 1
        with self.assertRaisesRegex(M.ToolError, "candidate_count"):
            M.validate_plan_for_apply(broken, client)

        broken = dict(plan)
        broken["base_url_origin"] = "http://different.invalid:80"
        with self.assertRaisesRegex(M.ToolError, "origin"):
            M.validate_plan_for_apply(broken, client)

        same_host_wrong_port = dict(plan)
        same_host_wrong_port["base_url_origin"] = "http://example.local:13081"
        with self.assertRaisesRegex(M.ToolError, "origin"):
            M.validate_plan_for_apply(same_host_wrong_port, client)

        broken = dict(plan)
        broken["assignments"] = list(plan["assignments"]) + [dict(plan["assignments"][0])]
        broken["candidate_count"] = len(broken["assignments"])
        with self.assertRaisesRegex(M.ToolError, "duplicate"):
            M.validate_plan_for_apply(broken, client)

        broken = dict(plan)
        assignment = dict(plan["assignments"][0])
        assignment["proxy_id"] = 999
        broken["assignments"] = [assignment] + list(plan["assignments"])[1:]
        with self.assertRaisesRegex(M.ToolError, "outside"):
            M.validate_plan_for_apply(broken, client)

    def test_precheck_frozen_field_drift_blocks_backup_and_writes(self) -> None:
        state = self._ready_state()
        client, plan = self._plan_and_client(state)
        state.accounts[1]["rate_limit_reset_at"] = "2026-07-29T00:00:00Z"
        backup_calls: list[list[str]] = []

        def must_not_backup(cmd, **kwargs):
            backup_calls.append(cmd)
            raise AssertionError("backup must not run after frozen state drift")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            artifacts = M.write_plan_artifacts(plan, tmp_path / "p")
            plan_path = Path(artifacts["plan_path"])
            loaded = json.loads(plan_path.read_text(encoding="utf-8"))
            calls_before = len(state.calls)
            with self.assertRaisesRegex(M.ToolError, "frozen state drift"):
                M.apply_plan(
                    client=client,
                    plan=loaded,
                    plan_path=plan_path,
                    confirm_production_write=True,
                    backup_dir=tmp_path / "bak",
                    postgres_container="pg",
                    pg_user="u",
                    pg_db="db",
                    run_command=must_not_backup,
                )
            writes = [
                call
                for call in state.calls[calls_before:]
                if call["method"] in {"POST", "PUT", "PATCH", "DELETE"}
            ]
            self.assertEqual(writes, [])
        self.assertEqual(backup_calls, [])

    def test_apply_call_order_and_no_test_or_delete(self) -> None:
        state = self._ready_state()
        client, plan = self._plan_and_client(state, seed=3)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            artifacts = M.write_plan_artifacts(plan, tmp_path / "p")
            loaded = json.loads(Path(artifacts["plan_path"]).read_text(encoding="utf-8"))

            dump_bytes = b"FAKE DUMP"

            def fake_run(cmd, **kwargs):
                if "pg_dump" in cmd:
                    stdout = kwargs.get("stdout")
                    if stdout is not None:
                        stdout.write(dump_bytes)
                    return SimpleNamespace(returncode=0, stderr=b"")
                if "pg_restore" in cmd:
                    return SimpleNamespace(returncode=0, stderr=b"")
                raise AssertionError(f"unexpected cmd {cmd}")

            calls_before = len(state.calls)
            summary = M.apply_plan(
                client=client,
                plan=loaded,
                plan_path=Path(artifacts["plan_path"]),
                confirm_production_write=True,
                backup_dir=tmp_path / "bak",
                postgres_container="sub2api-postgres",
                pg_user="sub2api",
                pg_db="sub2api",
                run_command=fake_run,
            )
            self.assertEqual(summary["failed"], 0)
            self.assertFalse(summary["quota_recovered_claimed"])
            self.assertEqual(summary["outcome"], "binding_applied")
            self.assertIsNotNone(summary["backup"])
            self.assertTrue(summary["backup"]["pg_restore_list_verified"])
            backup_path = Path(summary["backup"]["path"])
            self.assertTrue(backup_path.is_file())
            self.assertEqual(stat.S_IMODE(backup_path.stat().st_mode), 0o600)
            self.assertEqual(
                [path for path in backup_path.parent.iterdir() if path.name.startswith(".")],
                [],
            )

            post = state.calls[calls_before:]
            paths = [c["path"] for c in post]
            self.assertTrue(all("/test" not in p for p in paths))
            self.assertTrue(all(c["method"] != "DELETE" for c in post))
            self.assertTrue(all("temp-unschedulable" not in p for p in paths))
            self.assertTrue(all("clear-rate-limit" not in p for p in paths))
            self.assertTrue(all("clear-error" not in p for p in paths))

            # For each applied account, order must be:
            # GET (precheck) ... then GET, POST false, PUT proxy, POST true, GET verify
            # Find mutating sequences for account 1 and 2.
            for account_id in (1, 2):
                seq = [
                    c
                    for c in post
                    if f"/accounts/{account_id}" in c["path"]
                    and c["method"] in {"GET", "POST", "PUT"}
                ]
                # last 5 for successful apply after precheck GET
                # There is precheck GET then apply GET + mutations + verify
                methods_paths = [(c["method"], c["path"], c.get("body")) for c in seq]
                # Extract the apply phase after first GET
                self.assertGreaterEqual(len(seq), 6)
                # Find first POST schedulable false
                idx = next(
                    i
                    for i, c in enumerate(seq)
                    if c["method"] == "POST"
                    and c["path"].endswith("/schedulable")
                    and c["body"] == {"schedulable": False}
                )
                window = seq[idx - 1 : idx + 4]
                self.assertEqual(window[0]["method"], "GET")
                self.assertEqual(window[1]["body"], {"schedulable": False})
                self.assertEqual(window[2]["method"], "PUT")
                self.assertIn("proxy_id", window[2]["body"])
                self.assertEqual(window[3]["body"], {"schedulable": True})
                self.assertEqual(window[4]["method"], "GET")

            # accounts bound
            self.assertIn(state.accounts[1]["proxy_id"], (11, 12))
            self.assertIn(state.accounts[2]["proxy_id"], (11, 12))
            self.assertTrue(state.accounts[1]["schedulable"])
            self.assertTrue(state.accounts[2]["schedulable"])

    def test_failure_rollback_current_account_only(self) -> None:
        state = self._ready_state()
        # Force proxy set failure on account 1 after schedulable false
        state.fail_proxy_for.add(1)
        client, plan = self._plan_and_client(state, seed=5)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            artifacts = M.write_plan_artifacts(plan, tmp_path / "p")
            loaded = json.loads(Path(artifacts["plan_path"]).read_text(encoding="utf-8"))

            def fake_run(cmd, **kwargs):
                if "pg_dump" in cmd:
                    kwargs["stdout"].write(b"DUMP")
                    return SimpleNamespace(returncode=0, stderr=b"")
                if "pg_restore" in cmd:
                    return SimpleNamespace(returncode=0, stderr=b"")
                raise AssertionError(cmd)

            summary = M.apply_plan(
                client=client,
                plan=loaded,
                plan_path=Path(artifacts["plan_path"]),
                confirm_production_write=True,
                backup_dir=tmp_path / "bak",
                postgres_container="pg",
                pg_user="u",
                pg_db="db",
                run_command=fake_run,
                failure_limit=0,
            )
            self.assertEqual(summary["failed"], 1)
            self.assertEqual(summary["applied"], 1)
            self.assertEqual(summary["outcome"], "binding_partial_failure")
            # account 1 rolled back to empty proxy and schedulable true
            self.assertTrue(M.is_proxy_empty(state.accounts[1]["proxy_id"]))
            self.assertTrue(state.accounts[1]["schedulable"])
            # account 2 applied
            self.assertFalse(M.is_proxy_empty(state.accounts[2]["proxy_id"]))

    def test_after_get_state_drift_fails_and_rolls_back(self) -> None:
        state = FakeAdminState(
            accounts=[account(1), account(3, proxy_id=11)],
            proxies=[
                {"id": 11, "name": "p11", "status": "active"},
                {"id": 12, "name": "p12", "status": "active"},
            ],
        )
        state.mutate_after_enable[1] = lambda row: row.update(
            {"temp_unschedulable_until": "2026-07-28T01:00:00Z"}
        )
        client, plan = self._plan_and_client(state)

        def fake_run(cmd, **kwargs):
            if "pg_dump" in cmd:
                kwargs["stdout"].write(b"DUMP")
                return SimpleNamespace(returncode=0, stderr=b"")
            if "pg_restore" in cmd:
                return SimpleNamespace(returncode=0, stderr=b"")
            raise AssertionError(cmd)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            artifacts = M.write_plan_artifacts(plan, tmp_path / "p")
            plan_path = Path(artifacts["plan_path"])
            loaded = json.loads(plan_path.read_text(encoding="utf-8"))
            summary = M.apply_plan(
                client=client,
                plan=loaded,
                plan_path=plan_path,
                confirm_production_write=True,
                backup_dir=tmp_path / "bak",
                postgres_container="pg",
                pg_user="u",
                pg_db="db",
                run_command=fake_run,
            )
            self.assertEqual(summary["failed"], 1)
            self.assertEqual(summary["outcome"], "binding_failed")
            self.assertTrue(M.is_proxy_empty(state.accounts[1]["proxy_id"]))
            self.assertTrue(state.accounts[1]["schedulable"])

    def test_already_applied_idempotent(self) -> None:
        state = FakeAdminState(
            accounts=[
                account(1, proxy_id=11),  # already bound as planned
                account(2),
            ],
            proxies=[
                {"id": 11, "name": "p11", "status": "active"},
                {"id": 12, "name": "p12", "status": "active"},
            ],
        )
        client = M.AdminClient("http://example.local", "secret-key", http_do=state.http_do)
        # Craft plan manually so account 1 expects proxy 11
        planned_account_1 = account(1)
        plan = {
            "tool": M.TOOL_NAME,
            "version": M.PLAN_VERSION,
            "platform": "grok",
            "type": "oauth",
            "base_url_origin": "http://example.local:80",
            "group_id": 7,
            "seed": 1,
            "proxy_pool": [
                {"id": 11, "name": "p11", "status": "active"},
                {"id": 12, "name": "p12", "status": "active"},
            ],
            "candidate_count": 2,
            "assignments": [
                {
                    "account_id": 1,
                    "proxy_id": 11,
                    "before": M.non_secret_before(planned_account_1),
                },
                {
                    "account_id": 2,
                    "proxy_id": 12,
                    "before": M.non_secret_before(state.accounts[2]),
                },
            ],
        }
        body = {k: v for k, v in plan.items() if k != "plan_sha256"}
        plan["plan_sha256"] = M.sha256_json(body)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_path = tmp_path / "plan.json"
            M.atomic_write_json(plan_path, plan)
            M.atomic_write_bytes(tmp_path / "plan.sha256", (plan["plan_sha256"] + "\n").encode())

            def fake_run(cmd, **kwargs):
                if "pg_dump" in cmd:
                    kwargs["stdout"].write(b"DUMP")
                    return SimpleNamespace(returncode=0, stderr=b"")
                if "pg_restore" in cmd:
                    return SimpleNamespace(returncode=0, stderr=b"")
                raise AssertionError(cmd)

            calls_before = len(state.calls)
            summary = M.apply_plan(
                client=client,
                plan=plan,
                plan_path=plan_path,
                confirm_production_write=True,
                backup_dir=tmp_path / "bak",
                postgres_container="pg",
                pg_user="u",
                pg_db="db",
                run_command=fake_run,
            )
            self.assertEqual(summary["already_applied"], 1)
            self.assertEqual(summary["applied"], 1)
            self.assertEqual(summary["failed"], 0)
            # account 1 should not have been PUT for proxy during apply (only precheck GET)
            post = state.calls[calls_before:]
            account1_puts = [
                c
                for c in post
                if c["method"] == "PUT" and c["path"].endswith("/accounts/1")
            ]
            self.assertEqual(account1_puts, [])
            self.assertEqual(state.accounts[2]["proxy_id"], 12)

    def test_admin_client_blocks_forbidden_endpoints(self) -> None:
        state = self._ready_state()
        client = M.AdminClient("http://example.local", "secret-key", http_do=state.http_do)
        with self.assertRaises(M.ToolError):
            client.request("POST", "/api/v1/admin/accounts/1/test")
        with self.assertRaises(M.ToolError):
            client.request("DELETE", "/api/v1/admin/accounts/1/temp-unschedulable")
        with self.assertRaises(M.ToolError):
            client.request("POST", "/api/v1/admin/accounts/1/clear-rate-limit")

    def test_api_error_does_not_echo_body(self) -> None:
        def http_do(method, url, headers, body, timeout):
            raise M.urllib.error.HTTPError(url, 500, "nope", hdrs=None, fp=None)  # type: ignore[arg-type]

        # Use urllib.error.HTTPError properly
        import urllib.error

        def http_do2(method, url, headers, body, timeout):
            raise urllib.error.HTTPError(
                url, 500, "server error", hdrs=None, fp=io.BytesIO(b'{"token":"SECRET","message":"leak"}')
            )

        client = M.AdminClient("http://example.local", "secret-key", http_do=http_do2)
        with self.assertRaises(M.ToolError) as ctx:
            client.request("GET", "/api/v1/admin/accounts/1")
        msg = str(ctx.exception)
        self.assertNotIn("SECRET", msg)
        self.assertNotIn("token", msg.lower().replace("toolerror", ""))
        self.assertIn("HTTP 500", msg)

    def test_default_http_transport_disables_environment_proxies(self) -> None:
        captured_handlers: list[Any] = []

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"code":0,"data":{}}'

        class Opener:
            def open(self, request, timeout):
                return Response()

        def fake_build_opener(*handlers):
            captured_handlers.extend(handlers)
            return Opener()

        with mock.patch.object(M.urllib.request, "build_opener", side_effect=fake_build_opener):
            status, payload = M.default_http_do(
                "GET", "http://127.0.0.1:8080/api/v1/admin/accounts", {}, None, 1.0
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["code"], 0)
        proxy_handlers = [
            handler
            for handler in captured_handlers
            if isinstance(handler, M.urllib.request.ProxyHandler)
        ]
        self.assertEqual(len(proxy_handlers), 1)
        self.assertEqual(proxy_handlers[0].proxies, {})


class CliSmokeTests(unittest.TestCase):
    def test_parser_has_plan_and_apply(self) -> None:
        parser = M.build_parser()
        ns = parser.parse_args(
            [
                "plan",
                "--base-url",
                "http://x",
                "--admin-key-file",
                "/tmp/admin.key",
                "--group-id",
                "7",
                "--proxy-ids",
                "1,2",
                "--output-dir",
                "/tmp/out",
                "--seed",
                "1",
            ]
        )
        self.assertEqual(ns.command, "plan")
        ns2 = parser.parse_args(
            [
                "apply",
                "--plan",
                "/tmp/plan.json",
                "--base-url",
                "http://x",
                "--admin-key-file",
                "/tmp/admin.key",
                "--confirm-production-write",
                "--backup-dir",
                "/tmp/b",
                "--postgres-container",
                "pg",
                "--pg-user",
                "u",
                "--pg-db",
                "d",
            ]
        )
        self.assertTrue(ns2.confirm_production_write)

    def test_parser_rejects_plaintext_admin_key_option(self) -> None:
        parser = M.build_parser()
        with mock.patch.object(sys, "stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "plan",
                        "--base-url",
                        "http://x",
                        "--admin-api-key",
                        "plaintext",
                        "--group-id",
                        "7",
                        "--proxy-ids",
                        "1,2",
                        "--output-dir",
                        "/tmp/out",
                    ]
                )

    def test_admin_key_file_requires_0600(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "admin.key"
            key_path.write_text("super-secret\n", encoding="utf-8")
            os.chmod(key_path, 0o600)
            self.assertEqual(M.resolve_api_key(key_path), "super-secret")
            with mock.patch.dict(
                os.environ,
                {"SUB2API_ADMIN_KEY_FILE": str(key_path)},
                clear=True,
            ):
                self.assertEqual(M.resolve_api_key(None), "super-secret")
            os.chmod(key_path, 0o644)
            with self.assertRaises(M.ToolError):
                M.resolve_api_key(key_path)


if __name__ == "__main__":
    unittest.main()
