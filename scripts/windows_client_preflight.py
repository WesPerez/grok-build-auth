#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

from windows_client_common import (
    WindowsClientError,
    client_root_from_config,
    load_config,
    require_config,
    url_json,
)


def bridge_health_url(config: dict[str, object]) -> str:
    path = str(config.get("bridge_health_path") or "/health").strip()
    if not path.startswith("/") or path.startswith("//") or urlparse(path).scheme:
        raise WindowsClientError("bridge_health_path must be an absolute path")
    return str(config["cloudflare_api_base"]).rstrip("/") + path


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the grok-build-auth Windows client without registering")
    parser.add_argument("--config", required=True)
    parser.add_argument("--cdp", default="127.0.0.1:9222")
    parser.add_argument("--skip-cdp", action="store_true")
    parser.add_argument("--allow-unsupported-python", action="store_true")
    args = parser.parse_args()

    checks: list[dict[str, object]] = []
    try:
        if not args.allow_unsupported_python and sys.version_info[:2] not in {(3, 12), (3, 13)}:
            raise WindowsClientError("Python 3.12 or 3.13 is required")
        checks.append({"name": "python", "ok": True, "version": sys.version.split()[0]})

        config_path, config = load_config(args.config)
        root = client_root_from_config(config_path)
        require_config(
            config,
            "cloudflare_api_base", "cloudflare_api_key", "defaultDomains",
            "proxy", "cpa_remote_base", "cpa_remote_secret",
        )
        if config.get("cloudflare_auth_mode") != "bearer":
            raise WindowsClientError("cloudflare_auth_mode must be bearer")
        for key in ("cpa_push_enabled", "cpa_push_required", "cpa_require_probe_passed", "mint_required"):
            if config.get(key) is not True:
                raise WindowsClientError(f"{key} must be true")
        checks.append({"name": "config", "ok": True, "path": str(config_path)})

        for module in ("DrissionPage", "curl_cffi", "psutil"):
            if importlib.util.find_spec(module) is None:
                raise WindowsClientError(f"missing Python dependency: {module}")
        checks.append({"name": "dependencies", "ok": True})

        proxy = urlparse(str(config["proxy"]))
        if not proxy.hostname or not proxy.port:
            raise WindowsClientError("proxy must include host and port")
        with socket.create_connection((proxy.hostname, proxy.port), timeout=3):
            pass
        checks.append({"name": "proxy-listener", "ok": True, "endpoint": f"{proxy.hostname}:{proxy.port}"})

        health = url_json(bridge_health_url(config))
        if health.get("status") != "ok":
            raise WindowsClientError("bridge health is not ok")
        checks.append({"name": "bridge", "ok": True})

        if not args.skip_cdp:
            cdp = url_json(f"http://{args.cdp}/json/version")
            if not cdp.get("webSocketDebuggerUrl"):
                raise WindowsClientError("CDP endpoint has no webSocketDebuggerUrl")
            checks.append({"name": "edge-cdp", "ok": True, "endpoint": args.cdp})

        if not (root / "grok_register_ttk.py").is_file():
            raise WindowsClientError("grok_register_ttk.py is missing")
        print(json.dumps({"status": "pass", "checks": checks}, ensure_ascii=False))
        return 0
    except (WindowsClientError, OSError, ValueError) as exc:
        print(json.dumps({"status": "fail", "checks": checks, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
