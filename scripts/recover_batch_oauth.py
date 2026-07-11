#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from xconsole_client.xai_oauth import CLIPROXYAPI_GROK_BASE_URL, complete_build_oauth
from xconsole_client.security import set_restrictive_umask


def load_batch_module():
    path = PROJECT_DIR / "scripts" / "register_and_import.py"
    spec = importlib.util.spec_from_file_location("register_and_import_recovery", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load batch orchestrator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    set_restrictive_umask()
    parser = argparse.ArgumentParser(description="Recover OAuth exports for already-created batch accounts")
    parser.add_argument("--batch", required=True)
    parser.add_argument("--private-dir", default=str(PROJECT_DIR / "private"))
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()

    batch_module = load_batch_module()
    private_dir = Path(args.private_dir).expanduser().resolve()
    batch_dir = batch_module.resolve_resume_batch(private_dir, args.batch)
    manifest_path = batch_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    config = batch_module.load_env(private_dir / "runtime.env")
    proxy_pool = batch_module.load_proxy_pool(config.get("GROK_PROXY_POOL_FILE", ""), config)
    legacy_proxy = config.get("HTTPS_PROXY") or config.get("HTTP_PROXY") or ""
    recovered: list[Path] = []

    for attempt in manifest.get("attempts") or []:
        result_path = Path(str(attempt.get("result_file") or batch_dir / "results" / (Path(str(attempt.get("email") or "")).stem + ".json")))
        result_path = result_path.resolve()
        results_root = (batch_dir / "results").resolve()
        if results_root not in result_path.parents:
            raise RuntimeError("recovery result path is outside the batch directory")
        if not result_path.is_file():
            candidates = list((batch_dir / "results").glob(f"{str(attempt.get('email') or '').split('@')[0]}.json"))
            if not candidates:
                continue
            result_path = candidates[0]
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        rows = payload.get("results") or []
        if not rows:
            continue
        row = rows[0]
        ambiguous_old_gate = "missing success evidence" in str(row.get("error") or "")
        if (not row.get("account_created") and not ambiguous_old_gate) or row.get("cliproxyapi_auth"):
            continue
        email = str(row.get("email") or attempt.get("email") or "")
        password = str(row.get("password") or "")
        if not email or not password:
            continue
        auth_dir = batch_dir / "auth" / email.split("@", 1)[0]
        proxy_ref = str(attempt.get("proxy_ref") or "")
        proxy = (
            proxy_pool.url_for(proxy_ref)
            if proxy_pool.configured and proxy_ref and proxy_ref != "direct"
            else legacy_proxy
        )
        last_error: Exception | None = None
        for number in range(1, max(1, args.attempts) + 1):
            try:
                oauth = complete_build_oauth(
                    email,
                    password,
                    cliproxyapi_auth_dir=auth_dir,
                    cliproxyapi_base_url=CLIPROXYAPI_GROK_BASE_URL,
                    proxy=proxy,
                    yescaptcha_key=config.get("YESCAPTCHA_API_KEY", ""),
                    protocol=True,
                    playwright_fallback=True,
                    headless=True,
                )
                auth_path = Path(str(oauth.cliproxyapi_path or ""))
                auth_path = auth_path.resolve()
                if not auth_path.is_file() or (batch_dir / "auth").resolve() not in auth_path.parents:
                    raise RuntimeError("OAuth recovery produced no auth file")
                row.update({"cliproxyapi_auth": str(auth_path), "error": None})
                payload.update({"ok": True, "success_count": 1, "failure_count": 0})
                batch_module.atomic_json(result_path, payload)
                attempt.update({
                    "status": "registered",
                    "stage": "completed",
                    "auth_file": str(auth_path),
                    "result_file": str(result_path),
                    "error": None,
                    "recovered_oauth": True,
                })
                attempt.pop("failed_stage", None)
                attempt.pop("recovery_error", None)
                recovered.append(auth_path)
                break
            except Exception as exc:
                last_error = exc
                if number < args.attempts:
                    time.sleep(2 * number)
        if last_error is not None and not attempt.get("recovered_oauth"):
            attempt["recovery_error"] = str(last_error)

    auth_paths = [
        Path(str(item["auth_file"])).resolve()
        for item in manifest.get("attempts") or []
        if item.get("status") == "registered" and item.get("auth_file")
    ]
    auth_root = (batch_dir / "auth").resolve()
    if any(not path.is_file() or auth_root not in path.parents for path in auth_paths):
        raise RuntimeError("recovery auth path is outside the batch directory")
    if not auth_paths:
        batch_module.atomic_json(manifest_path, manifest)
        return 1
    bundle_path = batch_dir / "bundle" / "sub2api-bundle.json"
    batch_module.atomic_json(bundle_path, batch_module.build_bundle(
        auth_paths,
        config["GROK_ACCOUNT_BASE_URL"].rstrip("/"),
    ))
    manifest.update({
        "bundle": str(bundle_path),
        "bundle_sha256": batch_module.hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
        "successful_registrations": len(auth_paths),
        "failed_registrations": sum(1 for item in manifest.get("attempts") or [] if item.get("status") == "failed"),
        "status": "registered-not-imported",
        "error_summary": None,
    })
    batch_module.atomic_json(manifest_path, manifest)
    print(json.dumps({"batch_id": manifest["batch_id"], "recovered": len(recovered), "auth_files": len(auth_paths)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
