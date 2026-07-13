#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import secrets
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        try:
            parts = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise RuntimeError(f"invalid env value for {key} in {path}: {exc}") from exc
        values[key] = parts[0] if parts else ""
    return values


def write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        if temp.exists():
            temp.unlink()


def load_proxy_urls(project: Path) -> dict[str, str]:
    runtime = read_env(project / "private" / "runtime.env")
    pool = json.loads((project / "private" / "proxies.json").read_text(encoding="utf-8"))
    result: dict[str, str] = {}
    for item in pool.get("proxies", []):
        if not item.get("enabled", True):
            continue
        ref = str(item.get("ref") or "").strip()
        env_name = str(item.get("url_env") or "").strip()
        value = runtime.get(env_name, "").strip()
        if ref and value:
            result[ref] = value
    return result


def split_targets(total: int, routes: int) -> list[int]:
    base, remainder = divmod(total, routes)
    return [base + (1 if index < remainder else 0) for index in range(routes)]


def parse_summary(log_path: Path) -> dict[str, Any] | None:
    marker = "GROK_CLIENT_SUMMARY="
    summary = None
    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith(marker):
                summary = json.loads(line[len(marker):])
    return summary


def successful_account_ids(route_dir: Path) -> set[int]:
    account_ids: set[int] = set()
    success_path = route_dir / "successes.jsonl"
    if success_path.is_file():
        with success_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                    account_id = int(record.get("account_id"))
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
                if record.get("action") == "created" and record.get("probe") == "passed":
                    account_ids.add(account_id)
    checkpoint_path = route_dir / "cpa_reprobe_checkpoint.json"
    if checkpoint_path.is_file():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            checkpoint = {}
        if isinstance(checkpoint, dict):
            for record in checkpoint.values():
                if not isinstance(record, dict) or record.get("pushed") is not True:
                    continue
                if record.get("action") != "created" or record.get("probe") != "passed":
                    continue
                try:
                    account_ids.add(int(record.get("account_id")))
                except (ValueError, TypeError):
                    continue
    return account_ids


def reprobe_routes(
    *,
    python: Path,
    reprobe_script: Path,
    project: Path,
    env: dict[str, str],
    routes: list[dict[str, Any]],
) -> None:
    jobs: list[tuple[subprocess.Popen[bytes], Any]] = []
    for route in routes:
        route_dir = Path(route["route_dir"])
        if not any((route_dir / "cpa_pending").glob("xai-*.json")):
            continue
        log_path = route_dir / "reprobe.log"
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.chmod(log_path, 0o600)
        log_handle = os.fdopen(log_fd, "ab", buffering=0)
        process = subprocess.Popen(
            [
                str(python),
                "-u",
                str(reprobe_script),
                "--config",
                route["config_path"],
                "--workers",
                "1",
                "--attempts",
                "1",
                "--retry-delay",
                "0",
            ],
            cwd=project,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        jobs.append((process, log_handle))
    for process, log_handle in jobs:
        process.wait()
        log_handle.close()


def build_config(
    *,
    client_root: Path,
    bridge_base: str,
    management_key: str,
    domain: str,
    proxy: str,
    route_dir: Path,
    attempts: int,
    target: int,
    email_provider: str = "cloudflare",
    duckmail_domain: str = "",
) -> dict[str, Any]:
    return {
        "client_root": str(client_root),
        "cloudflare_api_base": bridge_base,
        "bridge_health_path": "/health",
        "cloudflare_api_key": management_key,
        "cloudflare_auth_mode": "bearer",
        "cloudflare_path_domains": "/api/domains",
        "cloudflare_path_accounts": "/admin/new_address",
        "cloudflare_path_token": "/api/token",
        "cloudflare_path_messages": "/api/mails",
        "email_provider": email_provider,
        "duckmail_domain": duckmail_domain,
        "defaultDomains": domain,
        "proxy": proxy,
        "register_count": attempts,
        "max_concurrency": 1,
        "target_successes": target,
        "accounts_output_dir": str(route_dir),
        "mail_credentials_file": str(route_dir / "mail_credentials.txt"),
        "success_records_file": str(route_dir / "successes.jsonl"),
        "enable_nsfw": False,
        "hide_window": False,
        "block_media_fonts": False,
        "stealth_patch": True,
        "cpa_export_enabled": True,
        "cpa_auth_dir": str(route_dir / "cpa_auths"),
        "cpa_base_url": "https://cli-chat-proxy.grok.com/v1",
        "cpa_push_enabled": True,
        "cpa_remote_base": bridge_base,
        "cpa_remote_secret": management_key,
        "cpa_remote_verify_tls": True,
        "cpa_push_proxy": "",
        "cpa_push_required": True,
        "cpa_require_probe_passed": True,
        "cpa_require_created": True,
        "cpa_push_timeout_sec": 960,
        "mint_proxy": "",
        "mint_timeout_sec": 420,
        "mint_required": True,
        "cpa_preprobe_enabled": True,
        "cpa_preprobe_required": True,
        "cpa_preprobe_timeout_sec": 60,
        "cpa_preprobe_refresh_on_invalid": True,
        "cpa_preprobe_attempts": 3,
        "cpa_preprobe_retry_delay_sec": 4,
        "cpa_preprobe_permission_retry_delay_sec": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run isolated Linux/Xvfb client-full Grok registration routes"
    )
    parser.add_argument("--project-dir", default="/root/grok-build-auth")
    parser.add_argument("--target", type=int, required=True)
    parser.add_argument("--routes", type=int, default=2)
    parser.add_argument("--attempts-per-route", type=int, default=200)
    parser.add_argument("--proxy-ref", action="append", dest="proxy_refs")
    parser.add_argument("--proxy-url", action="append", dest="proxy_urls")
    parser.add_argument(
        "--email-provider", choices=("cloudflare", "duckmail"), default="cloudflare"
    )
    parser.add_argument("--duckmail-domain", default="")
    parser.add_argument("--run-id")
    parser.add_argument("--reprobe-interval", type=int, default=300)
    args = parser.parse_args()

    project = Path(args.project_dir).resolve()
    if args.target < 1:
        raise SystemExit("--target must be at least 1")
    if args.routes < 1 or args.routes > 2:
        raise SystemExit("--routes must be 1 or 2")
    targets = split_targets(args.target, args.routes)
    if any(target > args.attempts_per_route for target in targets):
        raise SystemExit("--attempts-per-route must be at least each route target")

    bridge_env = read_env(project / "private" / "bridge.env")
    management_key_path = Path(bridge_env["BRIDGE_MANAGEMENT_KEY_FILE"])
    management_key = management_key_path.read_text(encoding="utf-8").strip()
    if not management_key:
        raise RuntimeError("bridge management key is empty")
    bridge_port = int(bridge_env.get("BRIDGE_PORT", "8190"))
    bridge_base = f"http://127.0.0.1:{bridge_port}"
    domain = bridge_env["MAILU_DOMAIN"]

    proxies = load_proxy_urls(project)
    if args.proxy_urls:
        if args.proxy_refs:
            raise RuntimeError("use either --proxy-ref or --proxy-url, not both")
        if len(args.proxy_urls) != args.routes or len(set(args.proxy_urls)) != args.routes:
            raise RuntimeError("provide one distinct --proxy-url per route")
        selected_routes = [
            (f"explicit-{index}", value.strip())
            for index, value in enumerate(args.proxy_urls, start=1)
        ]
        if any(not value for _, value in selected_routes):
            raise RuntimeError("--proxy-url cannot be empty")
    else:
        selected_refs = args.proxy_refs or list(proxies)[: args.routes]
        if len(selected_refs) != args.routes or len(set(selected_refs)) != args.routes:
            raise RuntimeError("provide one distinct --proxy-ref per route")
        missing = [ref for ref in selected_refs if ref not in proxies]
        if missing:
            raise RuntimeError(f"proxy refs are not configured/enabled: {missing}")
        selected_routes = [(ref, proxies[ref]) for ref in selected_refs]

    run_id = args.run_id or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(3)
    run_dir = project / "private" / "client-runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(run_dir, 0o700)
    manifest_path = run_dir / "manifest.json"
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "status": "preparing",
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "target": args.target,
        "routes": [],
    }
    write_private_json(manifest_path, manifest)

    python = project / "clients" / "windows" / ".venv" / "bin" / "python3"
    client = project / "clients" / "windows" / "grok_register_ttk.py"
    preflight = project / "scripts" / "windows_client_preflight.py"
    reprobe_script = project / "clients" / "windows" / "cpa_reprobe.py"
    env = os.environ.copy()
    env["DISPLAY"] = env.get("DISPLAY") or ":99"

    route_specs: list[dict[str, Any]] = []
    for index, ((ref, proxy_url), target) in enumerate(
        zip(selected_routes, targets), start=1
    ):
        route_dir = run_dir / f"route-{index}"
        route_dir.mkdir(mode=0o700)
        config_path = route_dir / "config.json"
        log_path = route_dir / "client.log"
        config = build_config(
            client_root=project / "clients" / "windows",
            bridge_base=bridge_base,
            management_key=management_key,
            domain=domain,
            proxy=proxy_url,
            route_dir=route_dir,
            attempts=args.attempts_per_route,
            target=target,
            email_provider=args.email_provider,
            duckmail_domain=args.duckmail_domain,
        )
        write_private_json(config_path, config)
        preflight_result = subprocess.run(
            [str(python), str(preflight), "--config", str(config_path), "--skip-cdp"],
            cwd=project,
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if preflight_result.returncode != 0:
            raise RuntimeError(
                f"route {index} preflight failed: {preflight_result.stdout.strip()}"
            )
        route_specs.append({
            "index": index,
            "proxy_ref": ref,
            "target": target,
            "config_path": str(config_path),
            "log_path": str(log_path),
            "route_dir": str(route_dir),
        })

    processes: list[tuple[subprocess.Popen[bytes], Any, dict[str, Any]]] = []
    for route in route_specs:
        log_fd = os.open(
            route["log_path"],
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        os.chmod(route["log_path"], 0o600)
        log_handle = os.fdopen(log_fd, "ab", buffering=0)
        command = [
            str(python),
            "-u",
            str(client),
            "--config",
            route["config_path"],
            "--count",
            str(args.attempts_per_route),
            "--target-successes",
            str(route["target"]),
            "--concurrency",
            "1",
            "--non-interactive",
        ]
        process = subprocess.Popen(
            command,
            cwd=project / "clients" / "windows",
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        route["pid"] = process.pid
        route["status"] = "running"
        processes.append((process, log_handle, route))

    manifest["status"] = "running"
    manifest["routes"] = route_specs
    write_private_json(manifest_path, manifest)

    next_reprobe = time.monotonic() + max(30, args.reprobe_interval)
    while any(process.poll() is None for process, _handle, _route in processes):
        for process, _handle, route in processes:
            account_ids = successful_account_ids(Path(route["route_dir"]))
            route["successful_account_ids"] = sorted(account_ids)
            if len(account_ids) >= int(route["target"]) and process.poll() is None:
                route["target_reached_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
                process.send_signal(signal.SIGINT)
        if time.monotonic() >= next_reprobe:
            reprobe_routes(
                python=python,
                reprobe_script=reprobe_script,
                project=project,
                env=env,
                routes=route_specs,
            )
            next_reprobe = time.monotonic() + max(30, args.reprobe_interval)
        time.sleep(5)

    reprobe_routes(
        python=python,
        reprobe_script=reprobe_script,
        project=project,
        env=env,
        routes=route_specs,
    )

    all_passed = True
    for process, log_handle, route in processes:
        log_handle.close()
        route["exit_code"] = process.returncode
        route["summary"] = parse_summary(Path(route["log_path"]))
        account_ids = successful_account_ids(Path(route["route_dir"]))
        route["successful_account_ids"] = sorted(account_ids)
        route["status"] = "passed" if len(account_ids) >= int(route["target"]) else "failed"
        if route["status"] != "passed":
            all_passed = False

    manifest["status"] = "completed" if all_passed else "failed"
    manifest["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    manifest["routes"] = route_specs
    write_private_json(manifest_path, manifest)
    print(json.dumps({
        "run_id": run_id,
        "status": manifest["status"],
        "target": args.target,
        "manifest": str(manifest_path),
    }, ensure_ascii=False))
    return 0 if all_passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
