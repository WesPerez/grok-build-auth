"""Fail-closed, non-x.ai health checks for configured registration proxies."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import secrets
import statistics
import time
from typing import Any, Callable

import requests

from .proxy_pool import ProxyPool, ProxyPoolError, ProxySpec


TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"


@dataclass(frozen=True)
class ProxyHealthSnapshot:
    checked_at: str
    results: tuple[dict[str, Any], ...]
    healthy_refs: tuple[str, ...]
    snapshot_sha256: str


def _parse_trace(text: str) -> dict[str, str]:
    fields = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key.strip()] = value.strip()
    ip = ipaddress.ip_address(fields.get("ip", ""))
    if not ip.is_global:
        raise ValueError("health endpoint returned a non-global exit IP")
    if not fields.get("tls", "").startswith("TLS"):
        raise ValueError("health endpoint did not confirm TLS")
    return {"ip": str(ip), "country": fields.get("loc", ""), "tls": fields["tls"]}


def _probe_once(spec: ProxySpec, timeout: float) -> tuple[str, str, str, float]:
    session = requests.Session()
    session.trust_env = False
    started = time.monotonic()
    response = session.get(
        TRACE_URL,
        proxies={"http": spec.url, "https": spec.url},
        timeout=timeout,
    )
    response.raise_for_status()
    parsed = _parse_trace(response.text)
    return parsed["ip"], parsed["country"], parsed["tls"], (time.monotonic() - started) * 1000


def check_proxy_pool_health(
    pool: ProxyPool,
    *,
    attempts: int = 3,
    timeout: float = 10.0,
    min_successes: int = 2,
    probe_once: Callable[[ProxySpec, float], tuple[str, str, str, float]] = _probe_once,
) -> ProxyHealthSnapshot:
    if not pool.configured:
        raise ProxyPoolError("proxy health check requires a configured pool")
    if attempts < 1 or min_successes < 1 or min_successes > attempts:
        raise ProxyPoolError("invalid proxy health attempt policy")
    salt = secrets.token_bytes(32)
    results: list[dict[str, Any]] = []
    exits: dict[str, str] = {}
    unhealthy: list[str] = []
    duplicates: list[str] = []
    for spec in pool.specs:
        samples = []
        errors = []
        for _ in range(attempts):
            try:
                samples.append(probe_once(spec, timeout))
            except Exception as exc:
                errors.append(type(exc).__name__)
        ips = {sample[0] for sample in samples}
        healthy = len(samples) >= min_successes and len(ips) == 1
        ip_value = next(iter(ips)) if len(ips) == 1 else ""
        digest = hashlib.sha256(salt + ip_value.encode()).hexdigest()[:16] if ip_value else ""
        if healthy and ip_value in exits:
            healthy = False
            duplicates.append(f"{spec.ref}={exits[ip_value]}")
        elif healthy:
            exits[ip_value] = spec.ref
        if not healthy:
            unhealthy.append(spec.ref)
        latencies = [sample[3] for sample in samples]
        results.append({
            "ref": spec.ref,
            "healthy": healthy,
            "attempts": attempts,
            "successes": len(samples),
            "stable_exit": len(ips) == 1 and bool(ips),
            "exit_hash": digest,
            "country": samples[0][1] if samples else "",
            "tls": samples[0][2] if samples else "",
            "latency_ms": {
                "min": round(min(latencies), 1),
                "median": round(statistics.median(latencies), 1),
                "max": round(max(latencies), 1),
            } if latencies else {},
            "errors": sorted(set(errors)),
        })
    if unhealthy:
        detail = f"unhealthy proxy refs: {sorted(unhealthy)}"
        if duplicates:
            detail += f"; duplicate exits: {sorted(duplicates)}"
        raise ProxyPoolError(detail)
    checked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    encoded = repr((checked_at, results)).encode()
    return ProxyHealthSnapshot(
        checked_at=checked_at,
        results=tuple(results),
        healthy_refs=tuple(item["ref"] for item in results if item["healthy"]),
        snapshot_sha256=hashlib.sha256(encoded).hexdigest(),
    )
