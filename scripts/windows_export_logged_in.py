#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from windows_client_common import (
    WindowsClientError,
    client_root_from_config,
    load_config,
    password_from_env,
    responses_probe,
    validate_bridge_result,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export OAuth from a logged-in Edge session and push it to Sub2API")
    parser.add_argument("--config", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password-env", default="GROK_ACCOUNT_PASSWORD")
    parser.add_argument("--cdp", default="127.0.0.1:9222")
    parser.add_argument("--require-created", action="store_true")
    parser.add_argument("--responses-base")
    parser.add_argument("--responses-key-env", default="GROK_GROUP_API_KEY")
    args = parser.parse_args()

    try:
        config_path, config = load_config(args.config)
        root = client_root_from_config(config_path)
        sys.path.insert(0, str(root))
        from DrissionPage import Chromium, ChromiumOptions
        import cpa_export

        password = password_from_env(args.password_env)
        options = ChromiumOptions()
        options.set_address(args.cdp)
        browser = Chromium(options)
        page = None
        for tab in browser.get_tabs():
            try:
                if "grok.com" in str(tab.url or "") and "accounts.x.ai" not in str(tab.url or ""):
                    page = tab
                    break
            except Exception:
                continue
        if page is None:
            raise WindowsClientError("no logged-in grok.com tab found on the Edge CDP session")
        page.get("https://grok.com")
        page.wait.doc_loaded()
        time.sleep(2)
        names = {
            str(item.get("name") or "")
            for item in (page.cookies(all_domains=True, all_info=True) or [])
            if isinstance(item, dict)
        }
        if not ({"sso", "sso-rw"} & names):
            raise WindowsClientError("the selected Edge tab has no sso/sso-rw cookie")

        result = cpa_export.export_cpa_for_account(
            args.email,
            password,
            page=page,
            config=config,
            log_callback=lambda message: print(message, file=sys.stderr, flush=True),
        )
        summary = validate_bridge_result(result, require_created=args.require_created)
        if args.responses_base:
            api_key = os.environ.get(args.responses_key_env, "")
            if not api_key:
                raise WindowsClientError(f"environment variable {args.responses_key_env} is not set")
            summary["responses_probe"] = responses_probe(args.responses_base, api_key)
        summary["status"] = "pass"
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except (WindowsClientError, OSError, ValueError) as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
