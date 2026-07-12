#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def pool_config_policy(path: Path, docker_listen: str) -> dict[str, bool]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"loopback": False, "docker_auth": False, "no_extra": False, "routing": False}
    inbounds = payload.get("inbounds") if isinstance(payload, dict) else None
    rules = (payload.get("routing") or {}).get("rules") if isinstance(payload, dict) else None
    if not isinstance(inbounds, list) or not isinstance(rules, list):
        return {"loopback": False, "docker_auth": False, "no_extra": False, "routing": False}

    by_endpoint = {
        (str(item.get("listen") or ""), item.get("port")): item
        for item in inbounds if isinstance(item, dict)
    }
    loopback_ok = all(("127.0.0.1", port) in by_endpoint for port in range(10900, 10908))
    docker_ok = True
    routing_ok = True
    for number, port in enumerate(range(10900, 10908), 1):
        item = by_endpoint.get((docker_listen, port))
        settings = item.get("settings") if isinstance(item, dict) else None
        accounts = settings.get("accounts") if isinstance(settings, dict) else None
        if (
            not isinstance(item, dict)
            or item.get("protocol") != "socks"
            or not isinstance(settings, dict)
            or settings.get("auth") != "password"
            or not isinstance(accounts, list) or not accounts
            or not all(
                isinstance(entry, dict) and entry.get("user") and entry.get("pass")
                for entry in accounts
            )
        ):
            docker_ok = False
        inbound_tag = f"in-docker-proxy-{number:02d}"
        outbound_tag = f"proxy-{number:02d}"
        if not any(
            isinstance(rule, dict)
            and rule.get("inboundTag") == [inbound_tag]
            and rule.get("outboundTag") == outbound_tag
            for rule in rules
        ):
            routing_ok = False
    allowed = {"127.0.0.1", docker_listen}
    no_extra = all(
        item.get("port") not in range(10900, 10908)
        or str(item.get("listen") or "") in allowed
        for item in inbounds if isinstance(item, dict)
    )
    return {"loopback": loopback_ok, "docker_auth": docker_ok, "no_extra": no_extra, "routing": routing_ok}


def listener_policy(tcp: str, pool_pid: str, docker_listen: str) -> dict[str, bool]:
    lines = tcp.splitlines()
    allowed = ("127.0.0.1", docker_listen)
    expected = all(
        any(f"{address}:{port}" in line and f"pid={pool_pid}," in line for line in lines)
        for address in allowed for port in range(10900, 10908)
    )
    no_extra = all(
        any(f"{address}:{port}" in line for address in allowed)
        for line in lines for port in range(10900, 10908) if f":{port}" in line
    )
    return {"expected": expected, "no_extra": no_extra}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify that the public V2Ray service and Grok pool are isolated")
    parser.add_argument("--main-config", default="/etc/v2ray/config.json")
    parser.add_argument("--pool-config", default="/etc/v2ray/grok_pool.json")
    parser.add_argument("--main-service", default="v2ray.service")
    parser.add_argument("--pool-service", default="v2ray-grok-pool.service")
    parser.add_argument("--docker-listen", default="172.18.0.1")
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

    config_policy = pool_config_policy(Path(args.pool_config), args.docker_listen)
    check("pool-loopback-config", config_policy["loopback"], "loopback inbounds")
    check("pool-docker-auth", config_policy["docker_auth"], "authenticated Docker inbounds")
    check("pool-no-public-inbound", config_policy["no_extra"], "no wildcard/public pool inbounds")
    check("pool-routing", config_policy["routing"], "numbered inbound/outbound mapping")

    active = run("systemctl", "is-active", args.main_service, args.pool_service).stdout.splitlines()
    check("main-active", len(active) >= 1 and active[0] == "active", args.main_service)
    check("pool-active", len(active) >= 2 and active[1] == "active", args.pool_service)

    udp = run("ss", "-lun").stdout
    tcp = run("ss", "-ltnpH").stdout
    check("public-vmess", ":31535" in udp, "udp/31535")
    check("main-local-socks", ":10808" in tcp and ":10810" in tcp, "10808,10810")
    pool_pid = run("systemctl", "show", "-p", "MainPID", "--value", args.pool_service).stdout.strip()
    listeners = listener_policy(tcp, pool_pid, args.docker_listen)
    check("grok-pool-listeners", listeners["expected"], "loopback and Docker listeners owned by pool PID")
    check("grok-pool-no-extra-listeners", listeners["no_extra"], "no wildcard/public pool listeners")

    passed = all(bool(item["ok"]) for item in checks)
    print(json.dumps({"status": "pass" if passed else "fail", "checks": checks}, ensure_ascii=False))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
