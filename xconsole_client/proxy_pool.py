"""Small, fail-closed proxy lease pool for batch registration."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import threading
from typing import Mapping
from urllib.parse import urlparse


class ProxyPoolError(RuntimeError):
    pass


PROXY_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class ProxySpec:
    ref: str
    url: str
    max_active_leases: int = 1
    sub2api_proxy_id: int | None = None


@dataclass(frozen=True)
class ProxyLease:
    spec: ProxySpec

    @property
    def ref(self) -> str:
        return self.spec.ref

    @property
    def url(self) -> str:
        return self.spec.url


class ProxyPool:
    def __init__(self, specs: list[ProxySpec], *, configured: bool) -> None:
        self._specs = specs
        self.configured = configured
        self._active = {spec.ref: 0 for spec in specs}
        self._cursor = 0
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return sum(spec.max_active_leases for spec in self._specs)

    @property
    def enabled_count(self) -> int:
        return len(self._specs)

    @property
    def specs(self) -> tuple[ProxySpec, ...]:
        return tuple(self._specs)

    def acquire(self) -> ProxyLease | None:
        if not self.configured:
            return None
        with self._lock:
            if not self._specs:
                raise ProxyPoolError("proxy pool is configured but has no enabled nodes")
            for offset in range(len(self._specs)):
                index = (self._cursor + offset) % len(self._specs)
                spec = self._specs[index]
                if self._active[spec.ref] < spec.max_active_leases:
                    self._active[spec.ref] += 1
                    self._cursor = (index + 1) % len(self._specs)
                    return ProxyLease(spec)
        raise ProxyPoolError("proxy pool has no available lease")

    def release(self, lease: ProxyLease | None) -> None:
        if lease is None:
            return
        with self._lock:
            active = self._active.get(lease.ref)
            if active is None or active < 1:
                raise ProxyPoolError(f"invalid proxy lease release: {lease.ref}")
            self._active[lease.ref] = active - 1

    def url_for(self, ref: str) -> str:
        for spec in self._specs:
            if spec.ref == ref:
                return spec.url
        raise ProxyPoolError(f"unknown proxy ref: {ref}")


def load_proxy_pool(path_value: str, values: Mapping[str, str]) -> ProxyPool:
    raw_path = (path_value or "").strip()
    if not raw_path:
        return ProxyPool([], configured=False)
    path = Path(raw_path).expanduser().resolve()
    if not path.is_file():
        raise ProxyPoolError(f"proxy pool file not found: {path}")
    if path.stat().st_mode & 0o077:
        raise ProxyPoolError(f"proxy pool permissions must be 0600: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProxyPoolError(f"invalid proxy pool JSON: {path}") from exc
    if payload.get("version") != 1 or not isinstance(payload.get("proxies"), list):
        raise ProxyPoolError("proxy pool requires version=1 and a proxies array")
    specs: list[ProxySpec] = []
    seen: set[str] = set()
    for item in payload["proxies"]:
        if not isinstance(item, dict) or item.get("enabled", True) is False:
            continue
        ref = str(item.get("ref") or "").strip()
        url_env = str(item.get("url_env") or "").strip()
        if not PROXY_REF_RE.fullmatch(ref) or ref in seen or not url_env:
            raise ProxyPoolError("each enabled proxy needs a unique ref and url_env")
        url = str(values.get(url_env) or os.environ.get(url_env) or "").strip()
        try:
            parsed = urlparse(url)
            valid_url = (
                parsed.scheme.lower() in {"http", "https", "socks5", "socks5h"}
                and bool(parsed.hostname) and parsed.port is not None
                and not any(char.isspace() for char in url)
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ProxyPoolError(f"proxy {ref} has no valid URL in {url_env}")
        try:
            limit = int(item.get("max_active_leases", 1))
        except (TypeError, ValueError) as exc:
            raise ProxyPoolError(f"proxy {ref} has invalid max_active_leases") from exc
        if limit < 1:
            raise ProxyPoolError(f"proxy {ref} max_active_leases must be at least 1")
        proxy_id_raw = item.get("sub2api_proxy_id")
        try:
            proxy_id = int(proxy_id_raw) if proxy_id_raw not in (None, "") else None
        except (TypeError, ValueError) as exc:
            raise ProxyPoolError(f"proxy {ref} has invalid sub2api_proxy_id") from exc
        if proxy_id is None:
            raise ProxyPoolError(f"proxy {ref} requires sub2api_proxy_id for post-import stickiness")
        if proxy_id < 1:
            raise ProxyPoolError(f"proxy {ref} sub2api_proxy_id must be positive")
        specs.append(ProxySpec(
            ref=ref,
            url=url,
            max_active_leases=limit,
            sub2api_proxy_id=proxy_id,
        ))
        seen.add(ref)
    return ProxyPool(specs, configured=True)
