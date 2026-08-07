#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--cdp", default="127.0.0.1:9222")
    parser.add_argument("--password-env", default="GROK_ACCOUNT_PASSWORD")
    parser.add_argument("--require-created", action="store_true")
    parser.add_argument("--responses-base")
    parser.add_argument("--responses-key-env", default="GROK_GROUP_API_KEY")
    args = parser.parse_args()
    script = Path(args.project_dir).resolve() / "scripts" / "windows_export_logged_in.py"
    command = [
        sys.executable, str(script), "--config", args.config, "--email", args.email,
        "--cdp", args.cdp, "--password-env", args.password_env,
    ]
    if args.require_created:
        command.append("--require-created")
    if args.responses_base:
        command.extend(["--responses-base", args.responses_base, "--responses-key-env", args.responses_key_env])
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
