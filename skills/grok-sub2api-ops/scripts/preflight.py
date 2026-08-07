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
    parser.add_argument("--cdp", default="127.0.0.1:9222")
    parser.add_argument("--skip-cdp", action="store_true")
    args = parser.parse_args()
    script = Path(args.project_dir).resolve() / "scripts" / "windows_client_preflight.py"
    command = [sys.executable, str(script), "--config", args.config, "--cdp", args.cdp]
    if args.skip_cdp:
        command.append("--skip-cdp")
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
