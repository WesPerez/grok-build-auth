#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify that the public V2Ray service and Grok pool are isolated")
    parser.add_argument("--main-config", default="/etc/v2ray/config.json")
    parser.add_argument("--pool-config", default="/etc/v2ray/grok_pool.json")
    parser.add_argument("--main-service", default="v2ray.service")
    parser.add_argument("--pool-service", default="v2ray-grok-pool.service")
    args = parser.parse_args()

    checks: list[dict[str, object]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    main_exec = run("systemctl", "show", "-p", "ExecStart", "--value", args.main_service).stdout.strip()
    pool_exec = run("systemctl", "show", "-p", "ExecStart", "--value", args.pool_service).stdout.strip()
    check("main-exec", args.main_config in main_exec and args.pool_config not in main_exec, args.main_service)
    check("pool-exec", args.pool_config in pool_exec, args.pool_service)

    dropin_dir = Path("/etc/systemd/system") / f"{args.main_service}.d"
    offenders = []
    if dropin_dir.is_dir():
        for path in dropin_dir.glob("*.conf"):
            try:
                if args.pool_config in path.read_text(encoding="utf-8"):
                    offenders.append(str(path))
            except OSError:
                offenders.append(str(path))
    check("main-dropins", not offenders, "none" if not offenders else ",".join(offenders))

    binary = "/usr/bin/v2ray/v2ray"
    for name, config in (("main-config", args.main_config), ("pool-config", args.pool_config)):
        result = run(binary, "test", "-config", config)
        check(name, result.returncode == 0, "valid" if result.returncode == 0 else "invalid")

    active = run("systemctl", "is-active", args.main_service, args.pool_service).stdout.splitlines()
    check("main-active", len(active) >= 1 and active[0] == "active", args.main_service)
    check("pool-active", len(active) >= 2 and active[1] == "active", args.pool_service)

    udp = run("ss", "-lun").stdout
    tcp = run("ss", "-ltn").stdout
    check("public-vmess", ":31535" in udp, "udp/31535")
    check("main-local-socks", ":10808" in tcp and ":10810" in tcp, "10808,10810")
    check(
        "grok-pool-loopback",
        all(f"127.0.0.1:{port}" in tcp for port in range(10900, 10908)),
        "127.0.0.1:10900-10907",
    )

    passed = all(bool(item["ok"]) for item in checks)
    print(json.dumps({"status": "pass" if passed else "fail", "checks": checks}, ensure_ascii=False))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
