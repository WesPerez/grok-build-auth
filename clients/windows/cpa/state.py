"""Atomic lifecycle management for local Grok auth candidates."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ContextManager, Iterator

from .schema import credential_file_name

STATES = {"verified": "cpa_auths", "pending": "cpa_pending", "cooldown": "cpa_cooldown"}


def identity(payload: dict[str, Any]) -> str:
    value = str(payload.get("email") or payload.get("sub") or "").strip().lower()
    if not value:
        raise ValueError("auth payload has no email/sub identity")
    return value


def fingerprint(payload: dict[str, Any]) -> str:
    token = str(payload.get("refresh_token") or "")
    return hashlib.sha256(token.encode()).hexdigest()


def push_fingerprint(payload: dict[str, Any]) -> str:
    material = json.dumps(
        {
            "access_token": str(payload.get("access_token") or ""),
            "refresh_token": str(payload.get("refresh_token") or ""),
            "sub": str(payload.get("sub") or ""),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()


@contextmanager
def _named_identity_lock(
    root: str | Path,
    key: str,
    directory: str,
    timeout: float,
) -> Iterator[None]:
    lock_dir = Path(root) / directory
    lock_dir.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(key.encode()).hexdigest() + ".lock"
    path = lock_dir / name
    deadline = time.monotonic() + timeout
    fd = None
    while fd is None:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"identity lock timeout: {key}")
            time.sleep(0.05)
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def identity_lock(root: str | Path, key: str, timeout: float = 15) -> ContextManager[None]:
    return _named_identity_lock(root, key, ".cpa_locks", timeout)


def operation_lock(root: str | Path, key: str, timeout: float = 300) -> ContextManager[None]:
    return _named_identity_lock(root, key, ".cpa_operation_locks", timeout)


def transition(
    root: str | Path,
    payload: dict[str, Any],
    target: str,
    *,
    verified_dir: str | Path | None = None,
    source_path: str | Path | None = None,
    expected_source_fingerprint: str | None = None,
) -> Path:
    if target not in STATES:
        raise ValueError(f"unknown state: {target}")
    root = Path(root).resolve()
    key = identity(payload)
    filename = credential_file_name(str(payload.get("email") or ""), str(payload.get("sub") or ""))
    with identity_lock(root, key):
        source = Path(source_path).resolve() if source_path else None
        if source is not None:
            if not source.is_file():
                raise RuntimeError("source auth changed before state transition")
            current_source = json.loads(source.read_text(encoding="utf-8"))
            if expected_source_fingerprint and fingerprint(current_source) != expected_source_fingerprint:
                raise RuntimeError("source auth changed before state transition")
            source_mtime = source.stat().st_mtime_ns
            for state_dir in (
                root / "cpa_pending",
                root / "cpa_cooldown",
                Path(verified_dir).resolve() if verified_dir else root / "cpa_auths",
            ):
                other = (state_dir / filename).resolve()
                if other == source or not other.is_file():
                    continue
                if other.stat().st_mtime_ns > source_mtime:
                    raise RuntimeError("newer auth state exists; refusing stale transition")
        dest_dir = Path(verified_dir).resolve() if target == "verified" and verified_dir else root / STATES[target]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename
        data = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=".xai-", suffix=".tmp", dir=dest_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data); handle.flush(); os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
        state_paths = [root / "cpa_pending", root / "cpa_cooldown", Path(verified_dir).resolve() if verified_dir else root / "cpa_auths"]
        for state_dir in state_paths:
            other = state_dir / filename
            if other != dest and other.exists():
                other.unlink()
        return dest


def delete_if_fingerprint(path: str | Path, expected: str, root: str | Path) -> bool:
    path = Path(path).resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    key = identity(payload)
    with identity_lock(root, key):
        current = json.loads(path.read_text(encoding="utf-8"))
        if fingerprint(current) != expected:
            return False
        path.unlink()
        return True


def replace_if_fingerprint(
    path: str | Path,
    expected: str,
    payload: dict[str, Any],
    root: str | Path,
) -> bool:
    path = Path(path).resolve()
    key = identity(payload)
    with identity_lock(root, key):
        current = json.loads(path.read_text(encoding="utf-8"))
        if fingerprint(current) != expected:
            return False
        data = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=".xai-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return True
