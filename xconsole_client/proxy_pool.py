"""Small, fail-closed proxy lease pool for batch registration."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import threading
from typing import Mapping
from urllib.parse import quote, urlparse

try:
    import fcntl
except ImportError:  # pragma: no cover - persistent rotation is Linux-only
    fcntl = None


class ProxyPoolError(RuntimeError):
    pass


PROXY_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _resin_proxy_url(config: Mapping[str, object], ref: str) -> str:
    scheme = str(config.get("scheme") or "socks5h").strip().lower()
    host = str(config.get("host") or "").strip()
    username = str(config.get("username") or "").strip()
    token_file_value = str(config.get("token_file") or "").strip()
    try:
        port = int(config.get("port") or 0)
    except (TypeError, ValueError) as exc:
        raise ProxyPoolError(f"proxy {ref} has invalid Resin port") from exc
    if scheme not in {"socks5", "socks5h"} or not host or port < 1 or port > 65535:
        raise ProxyPoolError(f"proxy {ref} has invalid Resin endpoint")
    if not username or any(char.isspace() or char in "@/:?#" for char in username):
        raise ProxyPoolError(f"proxy {ref} has invalid Resin username")
    if not token_file_value:
        raise ProxyPoolError(f"proxy {ref} requires a Resin token_file")
    token_file = Path(token_file_value).expanduser()
    if token_file.is_symlink() or not token_file.is_file():
        raise ProxyPoolError(f"proxy {ref} Resin token_file is missing or symlinked")
    if token_file.stat().st_mode & 0o077:
        raise ProxyPoolError(f"proxy {ref} Resin token_file permissions must be 0600 or stricter")
    token = token_file.read_text(encoding="utf-8").strip()
    if not token or any(char.isspace() for char in token):
        raise ProxyPoolError(f"proxy {ref} Resin token_file is empty or malformed")
    return (
        f"{scheme}://{quote(username, safe='')}:{quote(token, safe='')}@"
        f"{host}:{port}"
    )


@dataclass(frozen=True)
class ProxySpec:
    ref: str
    url: str
    max_active_leases: int = 1
    sub2api_proxy_id: int | None = None
    source: str = "legacy"


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
    def __init__(
        self, specs: list[ProxySpec], *, configured: bool,
        rotation_state_path: Path | None = None,
        schema_version: int | None = None,
    ) -> None:
        self._specs = specs
        self.configured = configured
        self.schema_version = schema_version
        self._active = {spec.ref: 0 for spec in specs}
        self._cursor = 0
        self._lock = threading.Lock()
        self._rotation_state_path = rotation_state_path

    @property
    def capacity(self) -> int:
        return sum(spec.max_active_leases for spec in self._specs)

    @property
    def enabled_count(self) -> int:
        return len(self._specs)

    @property
    def specs(self) -> tuple[ProxySpec, ...]:
        return tuple(self._specs)

    def only_refs(self, refs: set[str]) -> "ProxyPool":
        specs = [spec for spec in self._specs if spec.ref in refs]
        if not specs:
            raise ProxyPoolError("proxy pool has no healthy nodes")
        return ProxyPool(
            specs,
            configured=self.configured,
            rotation_state_path=self._rotation_state_path,
            schema_version=self.schema_version,
        )

    def acquire(self) -> ProxyLease | None:
        if not self.configured:
            return None
        with self._lock:
            if not self._specs:
                raise ProxyPoolError("proxy pool is configured but has no enabled nodes")
            lock_fd = self._lock_rotation_state()
            try:
                cursor = self._read_rotation_cursor() if lock_fd is not None else self._cursor
                for offset in range(len(self._specs)):
                    index = (cursor + offset) % len(self._specs)
                    spec = self._specs[index]
                    if self._active[spec.ref] < spec.max_active_leases:
                        next_cursor = (index + 1) % len(self._specs)
                        if lock_fd is not None:
                            self._write_rotation_cursor(next_cursor)
                        self._active[spec.ref] += 1
                        self._cursor = next_cursor
                        return ProxyLease(spec)
            finally:
                if lock_fd is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    os.close(lock_fd)
        raise ProxyPoolError("proxy pool has no available lease")

    def _lock_rotation_state(self) -> int | None:
        path = self._rotation_state_path
        if path is None:
            return None
        if fcntl is None:
            raise ProxyPoolError("persistent proxy rotation requires fcntl support")
        if not path.parent.is_dir():
            raise ProxyPoolError(f"proxy rotation state directory does not exist: {path.parent}")
        lock_path = path.with_name(path.name + ".lock")
        if lock_path.is_symlink():
            raise ProxyPoolError(f"proxy rotation lock must not be a symlink: {lock_path}")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        if os.fstat(fd).st_mode & 0o077:
            os.close(fd)
            raise ProxyPoolError(f"proxy rotation lock permissions must be 0600: {lock_path}")
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _read_rotation_cursor(self) -> int:
        path = self._rotation_state_path
        assert path is not None
        if not path.exists():
            return 0
        if path.stat().st_mode & 0o077:
            raise ProxyPoolError(f"proxy rotation state permissions must be 0600: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProxyPoolError(f"invalid proxy rotation state: {path}") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or set(payload) - {"version", "next_ref"}
        ):
            raise ProxyPoolError(f"invalid proxy rotation state: {path}")
        next_ref = str(payload.get("next_ref") or "")
        for index, spec in enumerate(self._specs):
            if spec.ref == next_ref:
                return index
        return 0

    def _write_rotation_cursor(self, cursor: int) -> None:
        path = self._rotation_state_path
        assert path is not None
        next_ref = self._specs[cursor % len(self._specs)].ref
        temp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            fd = os.open(temp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                payload = (json.dumps({"version": 1, "next_ref": next_ref}) + "\n").encode()
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temp_path, path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

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
        return ProxyPool([], configured=False, schema_version=None)
    path = Path(raw_path).expanduser().resolve()
    if not path.is_file():
        raise ProxyPoolError(f"proxy pool file not found: {path}")
    if path.stat().st_mode & 0o077:
        raise ProxyPoolError(f"proxy pool permissions must be 0600: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProxyPoolError(f"invalid proxy pool JSON: {path}") from exc
    version = payload.get("version")
    if version not in {1, 2} or not isinstance(payload.get("proxies"), list):
        raise ProxyPoolError("proxy pool requires version=1|2 and a proxies array")
    resin_only = version == 2
    specs: list[ProxySpec] = []
    seen: set[str] = set()
    seen_resin_usernames: set[str] = set()
    bind_after_import = str(values.get("GROK_BIND_SUB2API_PROXY_AFTER_IMPORT", "false")).strip().lower() in {
        "1", "true", "yes", "on",
    }
    allow_missing_proxy_ids = not bind_after_import or str(values.get("GROK_ALLOW_MISSING_SUB2API_PROXY_IDS", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }
    for item in payload["proxies"]:
        if not isinstance(item, dict) or item.get("enabled", True) is False:
            continue
        ref = str(item.get("ref") or "").strip()
        url_env = str(item.get("url_env") or "").strip()
        resin_config = item.get("resin")
        if not PROXY_REF_RE.fullmatch(ref) or ref in seen:
            raise ProxyPoolError("each enabled proxy needs a unique ref")
        if resin_config is not None:
            if url_env or not isinstance(resin_config, dict):
                raise ProxyPoolError(f"proxy {ref} has invalid Resin declaration")
            resin_username = str(resin_config.get("username") or "").strip()
            if resin_username in seen_resin_usernames:
                raise ProxyPoolError(
                    f"proxy {ref} reuses Resin logical identity {resin_username}"
                )
            url = _resin_proxy_url(resin_config, ref)
            seen_resin_usernames.add(resin_username)
            source = "resin"
        else:
            if resin_only:
                raise ProxyPoolError(f"proxy {ref} requires a Resin declaration in version=2")
            if not url_env:
                raise ProxyPoolError(f"proxy {ref} needs url_env or a Resin declaration")
            url = str(values.get(url_env) or os.environ.get(url_env) or "").strip()
            source = "legacy"
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
        if proxy_id is None and not allow_missing_proxy_ids:
            raise ProxyPoolError(f"proxy {ref} requires sub2api_proxy_id for post-import stickiness")
        if proxy_id is not None and proxy_id < 1:
            raise ProxyPoolError(f"proxy {ref} sub2api_proxy_id must be positive")
        specs.append(ProxySpec(
            ref=ref,
            url=url,
            max_active_leases=limit,
            sub2api_proxy_id=proxy_id,
            source=source,
        ))
        seen.add(ref)
    rotation_state_raw = str(values.get("GROK_PROXY_ROTATION_STATE_FILE") or "").strip()
    rotation_state_candidate = Path(rotation_state_raw).expanduser() if rotation_state_raw else None
    if rotation_state_candidate is not None and rotation_state_candidate.is_symlink():
        raise ProxyPoolError("proxy rotation state must not be a symlink")
    rotation_state_path = rotation_state_candidate.resolve() if rotation_state_candidate is not None else None
    if rotation_state_path is not None:
        if path.parent.stat().st_mode & 0o077:
            raise ProxyPoolError(f"proxy private directory permissions must be 0700: {path.parent}")
        if rotation_state_path.parent != path.parent:
            raise ProxyPoolError("proxy rotation state must be beside the proxy pool file")
        if rotation_state_path == path:
            raise ProxyPoolError("proxy rotation state must differ from the proxy pool file")
        if rotation_state_path.is_symlink():
            raise ProxyPoolError("proxy rotation state must not be a symlink")
        if rotation_state_path.exists():
            if rotation_state_path.stat().st_mode & 0o077:
                raise ProxyPoolError(f"proxy rotation state permissions must be 0600: {rotation_state_path}")
            try:
                state_payload = json.loads(rotation_state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ProxyPoolError(f"invalid proxy rotation state: {rotation_state_path}") from exc
            if (
                not isinstance(state_payload, dict)
                or state_payload.get("version") != 1
                or set(state_payload) - {"version", "next_ref"}
            ):
                raise ProxyPoolError(f"invalid proxy rotation state: {rotation_state_path}")
    return ProxyPool(
        specs,
        configured=True,
        rotation_state_path=rotation_state_path,
        schema_version=int(version),
    )
