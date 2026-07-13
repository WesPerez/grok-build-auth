#!/usr/bin/env python3
"""Concurrently reprobe Grok auths and resume failed bridge pushes."""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import cpa


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _push_error_code(body: str) -> str:
    try:
        payload = json.loads(body or "{}")
    except json.JSONDecodeError:
        return ""
    return str(payload.get("error_code") or "") if isinstance(payload, dict) else ""


def _bridge_metadata(body: str) -> dict[str, Any]:
    try:
        payload = json.loads(body or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        "action": payload.get("action"),
        "account_id": payload.get("account_id"),
        "probe": payload.get("probe"),
    }


def run(args: argparse.Namespace) -> dict[str, int]:
    root = Path(args.root).resolve()
    checkpoint_path = Path(args.checkpoint or root / "cpa_reprobe_checkpoint.json")
    checkpoint: dict[str, Any] = {}
    if checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint_lock = threading.Lock()
    include_verified = bool(getattr(args, "include_verified", False))
    workers = max(1, int(getattr(args, "workers", 8)))
    attempts = max(1, int(getattr(args, "attempts", 3)))
    retry_delay = max(0.0, float(getattr(args, "retry_delay", 2)))

    candidates = list((root / "cpa_pending").glob("xai-*.json"))
    candidates += list((root / "cpa_cooldown").glob("xai-*.json"))
    if include_verified:
        candidates += list((root / "cpa_auths").glob("xai-*.json"))
    # A stale duplicate may appear in more than one state directory. Select one
    # newest credential per identity before starting workers; path-level dedupe
    # alone allows two queued tasks to race and leave the wrong final state.
    by_identity: dict[str, Path] = {}
    for path in {item.resolve() for item in candidates}:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            key = cpa.identity(payload)
        except Exception:
            key = "invalid:" + str(path)
        current = by_identity.get(key)
        if current is None or path.stat().st_mtime_ns > current.stat().st_mtime_ns:
            by_identity[key] = path
    paths = sorted(by_identity.values(), key=str)

    def save(path: Path, record: dict[str, Any]) -> None:
        with checkpoint_lock:
            checkpoint[str(path)] = record
            _atomic_json(checkpoint_path, checkpoint)

    def new_result() -> dict[str, int]:
        return {"passed": 0, "cooldown": 0, "pending": 0, "revoked_deleted": 0,
                "revoked_preserved": 0, "verified_skipped": 0, "errors": 0,
                "pushed": 0, "push_failed": 0, "push_skipped": 0,
                "stale_rejected": 0, "terminal_skipped": 0}

    def process_locked(path: Path) -> dict[str, int]:
        result = new_result()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            fp = cpa.fingerprint(payload)
            with checkpoint_lock:
                old = dict(checkpoint.get(str(path), {}))
            is_verified = path.parent.name == "cpa_auths"
            push_fp = cpa.push_fingerprint(payload)
            checkpoint_fp = old.get("push_fingerprint") or old.get("fingerprint")
            if old.get("terminal_error") and checkpoint_fp == push_fp:
                result["terminal_skipped"] = 1
                result["push_skipped"] = 1
                return result
            if is_verified:
                if checkpoint_fp == push_fp and old.get("pushed") is True:
                    result["passed"] = 1
                    result["push_skipped"] = 1
                    return result
                if checkpoint_fp != push_fp or old.get("pushed") is not False:
                    result["verified_skipped"] = 1
                    result["push_skipped"] = 1
                    return result

            probe = {}
            for attempt in range(1, attempts + 1):
                probe = cpa.probe_auth(payload, proxy=args.proxy, timeout=args.timeout)
                probe["attempts"] = attempt
                if probe.get("decision") != "retry":
                    break
                if probe.get("code") not in {
                    "PROBE_NETWORK_ERROR", "INVALID_RESPONSE", "INCOMPLETE_RESPONSE",
                    "UPSTREAM_ERROR", "PERMISSION_DENIED",
                }:
                    break
                if attempt < attempts:
                    time.sleep(retry_delay)
            if probe["decision"] == "refresh":
                refreshed = cpa.try_refresh_access_token(payload, proxy=args.proxy, timeout=args.timeout)
                if refreshed.get("ok"):
                    payload = refreshed["auth"]
                    if not cpa.replace_if_fingerprint(path, fp, payload, root):
                        result["errors"] = 1
                        save(path, {"fingerprint": fp, "code": "AUTH_CHANGED_DURING_REFRESH"})
                        return result
                    fp = cpa.fingerprint(payload)
                    probe = cpa.probe_auth(payload, proxy=args.proxy, timeout=args.timeout)
                elif refreshed.get("code") == "TOKEN_INVALID":
                    result["revoked_preserved"] = 1
                    save(path, {
                        "fingerprint": fp,
                        "push_fingerprint": push_fp,
                        "code": "TOKEN_INVALID",
                        "revoked": True,
                        "pushed": old.get("pushed"),
                    })
                    return result

            decision = str(probe.get("decision") or "")
            promoted = path
            if decision == "pass":
                if not is_verified:
                    promoted = cpa.transition(
                        root,
                        payload,
                        "verified",
                        source_path=path,
                        expected_source_fingerprint=fp,
                    )
                result["passed"] = 1
                pushed: bool | None = None
                terminal_error = ""
                bridge_meta: dict[str, Any] = {}
                if args.remote_base and args.remote_secret:
                    ok, status, response_body = cpa.push_auth_file(
                        remote_base=args.remote_base, secret=args.remote_secret,
                        filename=promoted.name, payload=payload, proxy=args.push_proxy or None,
                        verify_tls=not args.insecure, timeout=args.push_timeout,
                    )
                    bridge_meta = _bridge_metadata(response_body)
                    if ok and bool(getattr(args, "require_created", False)):
                        if bridge_meta.get("action") != "created":
                            ok = False
                            terminal_error = "BRIDGE_ACTION_NOT_CREATED"
                    pushed = bool(ok)
                    result["pushed" if ok else "push_failed"] = 1
                    probe["push_status"] = status
                    terminal_error = terminal_error or _push_error_code(response_body)
                    if terminal_error == "STALE_AUTH":
                        result["stale_rejected"] = 1
                else:
                    result["push_skipped"] = 1
                save(promoted, {
                    "fingerprint": cpa.fingerprint(payload),
                    "push_fingerprint": cpa.push_fingerprint(payload),
                    "code": probe.get("code"),
                    "decision": decision,
                    "pushed": pushed,
                    "terminal_error": terminal_error or None,
                    **bridge_meta,
                })
            elif decision == "cooldown":
                pushed = False
                status = 0
                terminal_error = ""
                push_cooldown = bool(getattr(args, "push_cooldown", False))
                if push_cooldown and args.remote_base and args.remote_secret:
                    ok, status, response_body = cpa.push_auth_file(
                        remote_base=args.remote_base, secret=args.remote_secret,
                        filename=path.name, payload=payload, proxy=args.push_proxy or None,
                        verify_tls=not args.insecure, timeout=args.push_timeout,
                    )
                    pushed = bool(ok)
                    terminal_error = _push_error_code(response_body)
                    if terminal_error == "STALE_AUTH":
                        result["stale_rejected"] = 1
                if pushed:
                    promoted = cpa.transition(
                        root,
                        payload,
                        "verified",
                        source_path=path,
                        expected_source_fingerprint=fp,
                    )
                    result["passed"] = 1
                    result["pushed"] = 1
                    save(promoted, {
                        "fingerprint": cpa.fingerprint(payload),
                        "push_fingerprint": cpa.push_fingerprint(payload),
                        "code": probe.get("code"),
                        "decision": decision,
                        "availability": "usable_exhausted",
                        "push_status": status,
                        "pushed": True,
                    })
                else:
                    if not is_verified:
                        cpa.transition(
                            root,
                            payload,
                            "cooldown",
                            source_path=path,
                            expected_source_fingerprint=fp,
                        )
                    result["cooldown"] = 1
                    if push_cooldown and args.remote_base and args.remote_secret:
                        result["push_failed"] = 1
                    save(path, {
                        "fingerprint": cpa.fingerprint(payload),
                        "push_fingerprint": cpa.push_fingerprint(payload),
                        "code": probe.get("code"),
                        "decision": decision,
                        "push_status": status or None,
                        "pushed": False if push_cooldown else None,
                        "terminal_error": terminal_error or None,
                    })
            else:
                # A transient verified-account failure must not demote or delete it.
                if not is_verified:
                    cpa.transition(
                        root,
                        payload,
                        "pending",
                        source_path=path,
                        expected_source_fingerprint=fp,
                    )
                result["pending"] = 1
                save(path, {
                    "fingerprint": cpa.fingerprint(payload),
                    "push_fingerprint": cpa.push_fingerprint(payload),
                    "code": probe.get("code"),
                    "decision": decision,
                    "pushed": old.get("pushed") if is_verified else None,
                })
        except Exception as exc:  # noqa: BLE001
            result["errors"] = 1
            save(path, {"error_type": type(exc).__name__})
        return result

    def process(path: Path) -> dict[str, int]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            key = cpa.identity(payload)
            lock_timeout = max(300.0, attempts * (float(args.timeout) + retry_delay) + float(args.push_timeout) + 60.0)
            with cpa.operation_lock(root, key, timeout=lock_timeout):
                return process_locked(path)
        except Exception as exc:  # noqa: BLE001
            result = new_result()
            result["errors"] = 1
            save(path, {"error_type": type(exc).__name__})
            return result

    stats = {"scanned": len(paths), "passed": 0, "pushed": 0, "push_failed": 0,
             "push_skipped": 0, "cooldown": 0, "pending": 0, "revoked_deleted": 0,
             "revoked_preserved": 0, "verified_skipped": 0, "errors": 0,
             "stale_rejected": 0, "terminal_skipped": 0}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(process, path) for path in paths]
        for future in as_completed(futures):
            for key, value in future.result().items():
                stats[key] += value
    classified = sum(stats[key] for key in (
        "passed", "cooldown", "pending", "revoked_deleted", "revoked_preserved",
        "verified_skipped", "terminal_skipped", "errors",
    ))
    if classified != stats["scanned"]:
        raise RuntimeError(f"statistics invariant failed: scanned={stats['scanned']} classified={classified}")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="client config; keeps bridge secrets out of the command line")
    parser.add_argument("--root", help="parent directory containing cpa_auths/cpa_pending/cpa_cooldown")
    parser.add_argument("--checkpoint", default="")
    parser.add_argument("--workers", type=int, default=8, help="parallel probes/pushes (use 15 for registration-sized batches)")
    parser.add_argument("--attempts", type=int, default=3, help="bounded probe attempts per auth")
    parser.add_argument("--retry-delay", type=float, default=2, help="seconds between retryable probe attempts")
    parser.add_argument("--include-verified", action="store_true", help="retry unconfirmed pushes from cpa_auths")
    parser.add_argument("--push-cooldown", action="store_true", help="push authenticated 402/429 accounts; bridge must accept usable_exhausted")
    parser.add_argument("--require-created", action="store_true", default=None)
    parser.add_argument("--proxy")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--remote-base")
    parser.add_argument("--remote-secret")
    parser.add_argument("--push-proxy")
    parser.add_argument("--push-timeout", type=float)
    parser.add_argument("--insecure", action="store_true", default=None)
    args = parser.parse_args()
    config: dict[str, Any] = {}
    if args.config:
        config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise SystemExit("client config root must be an object")
    if not args.root:
        auth_dir = str(config.get("cpa_auth_dir") or "").strip()
        if not auth_dir:
            raise SystemExit("--root or config cpa_auth_dir is required")
        args.root = str(Path(auth_dir).expanduser().resolve().parent)
    if args.proxy is None:
        args.proxy = str(config.get("mint_proxy") or config.get("proxy") or "")
    if args.timeout is None:
        args.timeout = float(config.get("cpa_preprobe_timeout_sec", 45) or 45)
    if args.remote_base is None:
        args.remote_base = str(config.get("cpa_remote_base") or "")
    if args.remote_secret is None:
        args.remote_secret = str(config.get("cpa_remote_secret") or "")
    if args.push_proxy is None:
        args.push_proxy = str(config.get("cpa_push_proxy") or "")
    if args.push_timeout is None:
        args.push_timeout = float(config.get("cpa_push_timeout_sec", 240) or 240)
    if args.insecure is None:
        args.insecure = not bool(config.get("cpa_remote_verify_tls", True))
    if args.require_created is None:
        args.require_created = bool(config.get("cpa_require_created", False))
    print(json.dumps(run(args), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
