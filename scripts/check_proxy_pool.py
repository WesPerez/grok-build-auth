#!/usr/bin/env python3
"""Check configured proxy nodes without contacting x.ai."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.register_and_import import load_env
from xconsole_client.proxy_health import check_proxy_pool_health
from xconsole_client.proxy_pool import load_proxy_pool


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-dir", default=str(Path(__file__).resolve().parents[1] / "private"))
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    private_dir = Path(args.private_dir).expanduser().resolve()
    config = load_env(private_dir / "runtime.env")
    pool = load_proxy_pool(config.get("GROK_PROXY_POOL_FILE", ""), config)
    snapshot = check_proxy_pool_health(pool, attempts=args.attempts, timeout=args.timeout)
    print(json.dumps({
        "checked_at": snapshot.checked_at,
        "healthy_refs": snapshot.healthy_refs,
        "snapshot_sha256": snapshot.snapshot_sha256,
        "results": snapshot.results,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
