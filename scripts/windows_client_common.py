from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class WindowsClientError(RuntimeError):
    pass


def load_config(path: str | Path) -> tuple[Path, dict[str, Any]]:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise WindowsClientError(f"config not found: {config_path}")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise WindowsClientError(f"invalid config JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise WindowsClientError("config root must be an object")
    return config_path, data


def require_config(config: dict[str, Any], *names: str) -> None:
    missing = []
    for name in names:
        value = str(config.get(name) or "").strip()
        if not value or value.startswith("<"):
            missing.append(name)
    if missing:
        raise WindowsClientError("missing config values: " + ", ".join(missing))


def client_root_from_config(config_path: Path) -> Path:
    root = config_path.parent
    if not (root / "cpa_export.py").is_file():
        raise WindowsClientError(f"Windows client modules not found beside {config_path}")
    return root


def safe_result(result: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "ok", "email", "path", "pushed", "push_status", "push_response",
        "error", "push_error", "skipped", "reason",
    }
    return {key: result[key] for key in allowed if key in result}


def validate_bridge_result(
    result: dict[str, Any], *, require_created: bool = False,
) -> dict[str, Any]:
    if not result.get("ok"):
        raise WindowsClientError(str(result.get("error") or "OAuth/export failed"))
    auth_path = Path(str(result.get("path") or "")).expanduser()
    if not auth_path.is_file():
        raise WindowsClientError("OAuth completed without an auth file")
    if not result.get("pushed"):
        raise WindowsClientError(str(result.get("push_error") or "auth was not pushed"))
    response = result.get("push_response")
    if not isinstance(response, dict) or response.get("probe") != "passed":
        raise WindowsClientError("bridge did not return probe=passed")
    if require_created and response.get("action") != "created":
        raise WindowsClientError(
            f"bridge action is {response.get('action')!r}, not a new account"
        )
    return safe_result(result)


def password_from_env(name: str, *, prompt: bool = True) -> str:
    value = os.environ.get(name, "")
    if value:
        return value
    if not prompt:
        raise WindowsClientError(f"environment variable {name} is not set")
    import getpass

    value = getpass.getpass(f"Account password ({name}): ")
    if not value:
        raise WindowsClientError("account password is empty")
    return value


def responses_probe(
    base_url: str, api_key: str, *, model: str = "grok-4.5", timeout: float = 60,
) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/v1/responses"
    payload = json.dumps({
        "model": model,
        "input": "Reply exactly: WINDOWS_CLIENT_OK",
        "max_output_tokens": 16,
        "store": False,
    }).encode()
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(response.status)
            body = response.read(1024 * 1024).decode(errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read(4096).decode(errors="replace")
        raise WindowsClientError(f"Responses probe HTTP {exc.code}: {body[:300]}") from exc
    try:
        data = json.loads(body)
    except Exception as exc:
        raise WindowsClientError("Responses probe returned invalid JSON") from exc
    text = json.dumps(data, ensure_ascii=False)
    if status != 200 or data.get("status") != "completed" or "WINDOWS_CLIENT_OK" not in text:
        raise WindowsClientError(f"Responses probe failed: HTTP {status}, status={data.get('status')}")
    return {"status": status, "completed": True, "output_ok": True}


def url_json(url: str, *, timeout: float = 10) -> dict[str, Any]:
    context = ssl.create_default_context()
    with urllib.request.urlopen(url, timeout=timeout, context=context) as response:
        data = json.loads(response.read(1024 * 1024))
    if not isinstance(data, dict):
        raise WindowsClientError(f"expected JSON object from {url}")
    return data
