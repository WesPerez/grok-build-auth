#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from windows_client_common import (
    WindowsClientError,
    client_root_from_config,
    load_config,
    responses_probe,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Push an existing xAI auth JSON through the hardened bridge")
    parser.add_argument("--config", required=True)
    parser.add_argument("--auth", required=True)
    parser.add_argument("--require-created", action="store_true")
    parser.add_argument("--responses-base")
    parser.add_argument("--responses-key-env", default="GROK_GROUP_API_KEY")
    args = parser.parse_args()

    try:
        config_path, config = load_config(args.config)
        root = client_root_from_config(config_path)
        sys.path.insert(0, str(root))
        import cpa

        auth_path = Path(args.auth).expanduser().resolve()
        payload = json.loads(auth_path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise WindowsClientError("auth JSON root must be an object")
        for key in ("email", "access_token", "refresh_token"):
            if not str(payload.get(key) or "").strip():
                raise WindowsClientError(f"auth JSON missing {key}")
        ok, status, text = cpa.push_auth_file(
            remote_base=str(config.get("cpa_remote_base") or ""),
            secret=str(config.get("cpa_remote_secret") or ""),
            filename=auth_path.name,
            payload=payload,
            proxy=str(config.get("cpa_push_proxy") or "").strip() or None,
            verify_tls=bool(config.get("cpa_remote_verify_tls", True)),
            timeout=float(config.get("cpa_push_timeout_sec", 240) or 240),
        )
        if not ok:
            raise WindowsClientError(f"bridge HTTP {status}: {text[:300]}")
        response = json.loads(text)
        if response.get("probe") != "passed":
            raise WindowsClientError("bridge did not return probe=passed")
        if args.require_created and response.get("action") != "created":
            raise WindowsClientError(f"bridge action is {response.get('action')!r}, not created")
        summary: dict[str, object] = {
            "status": "pass", "auth": str(auth_path), "push_status": status,
            "account_id": response.get("account_id"), "action": response.get("action"),
            "probe": response.get("probe"),
        }
        if args.responses_base:
            api_key = os.environ.get(args.responses_key_env, "")
            if not api_key:
                raise WindowsClientError(f"environment variable {args.responses_key_env} is not set")
            summary["responses_probe"] = responses_probe(args.responses_base, api_key)
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except (WindowsClientError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
