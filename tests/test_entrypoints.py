from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import xai_build_quota_probe as quota


ROOT = Path(__file__).resolve().parents[1]


class EntrypointTests(unittest.TestCase):
    def test_server_count_and_threads_are_limited(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "run.py"), "-n", "2"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid choice", result.stderr)

    def test_quota_probe_refuses_redirects(self):
        response = mock.Mock(status_code=302)
        with mock.patch.object(quota.requests, "post", return_value=response) as post:
            with mock.patch.object(Path, "read_text", return_value='{"access_token":"secret"}'):
                with self.assertRaisesRegex(RuntimeError, "redirect refused"):
                    quota.probe(Path("xai-test.json"), timeout=1)
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertEqual(post.call_args.args[0], "https://cli-chat-proxy.grok.com/v1/responses")


if __name__ == "__main__":
    unittest.main()
