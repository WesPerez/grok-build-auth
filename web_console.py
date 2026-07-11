#!/usr/bin/env python3
"""Local web console for batch registration and Sub2API import."""
from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
import datetime as dt
from urllib.parse import urlparse

from xconsole_client.proxy_pool import ProxyPoolError, load_proxy_pool


PROJECT_DIR = Path(__file__).resolve().parent
PRIVATE_DIR = PROJECT_DIR / "private"
RUNS_DIR = PRIVATE_DIR / "runs"
WEB_DIR = PROJECT_DIR / "web"
TASK_STATE_PATH = PRIVATE_DIR / "web" / "current-task.json"
ACTIVE_BATCH_STATUSES = {"running", "importing", "resuming-import"}
DOCTOR_CACHE_TTL = 30.0


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def safe_batch_id(value: str) -> str:
    if not value or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in value):
        raise ValueError("invalid batch ID")
    return value


def batch_summary(path: Path) -> dict:
    manifest = read_json(path / "manifest.json")
    attempts = manifest.get("attempts") if isinstance(manifest.get("attempts"), list) else []
    manifest["attempts"] = attempts
    manifest["registered_count"] = sum(item.get("status") == "registered" for item in attempts)
    manifest["failed_count"] = sum(item.get("status") == "failed" for item in attempts)
    manifest["running_count"] = sum(item.get("status") == "running" for item in attempts)
    manifest["resumable"] = manifest.get("status") in {
        "import-failed", "import-verification-failed", "registered-not-imported",
        "registration-failed-import-skipped",
    }
    manifest["has_backup"] = any((path / "backup").glob("*.dump"))
    manifest["runtime_state"] = manifest.get("status")
    return manifest


def list_batches() -> list[dict]:
    if not RUNS_DIR.is_dir():
        return []
    batches = [batch_summary(path) for path in RUNS_DIR.iterdir() if (path / "manifest.json").is_file()]
    return sorted((item for item in batches if item.get("batch_id")), key=lambda item: item["batch_id"], reverse=True)


class TaskManager:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.task: dict | None = read_json(TASK_STATE_PATH) or None

    @staticmethod
    def _process_identity(pid: int) -> tuple[str, str]:
        try:
            started = Path(f"/proc/{pid}/stat").read_text().split()[21]
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            return started, command
        except (OSError, IndexError):
            return "", ""

    def _save(self) -> None:
        if not self.task:
            return
        TASK_STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = TASK_STATE_PATH.with_name(f".{TASK_STATE_PATH.name}.{secrets.token_hex(4)}.tmp")
        temp.write_text(json.dumps(self.task, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.chmod(0o600)
        os.replace(temp, TASK_STATE_PATH)

    def _restored_process_alive(self) -> bool:
        if not self.task:
            return False
        started, command = self._process_identity(int(self.task.get("pid") or 0))
        return bool(
            started and started == self.task.get("process_started_ticks")
            and str(PROJECT_DIR / "scripts/register_and_import.py") in command
        )

    def current(self) -> dict | None:
        with self.lock:
            if self.process is not None and self.task is not None:
                code = self.process.poll()
                self.task["running"] = code is None
                self.task["exit_code"] = code
                if code is not None and not self.task.get("ended_at"):
                    self.task["ended_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
                self._save()
            elif self.task is not None and self.task.get("running"):
                self.task["running"] = self._restored_process_alive()
                if not self.task["running"]:
                    self.task["interrupted"] = True
                    self.task["ended_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
                    self._save()
            return dict(self.task) if self.task else None

    def start(self, command: list[str], kind: str, subject: str, batch_id: str = "") -> dict:
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                raise RuntimeError("another task is already running")
            if self.task is not None and self.task.get("running") and self._restored_process_alive():
                raise RuntimeError("another restored task is already running")
            task_id = secrets.token_hex(6)
            task_dir = PRIVATE_DIR / "web" / "tasks"
            task_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            log_path = task_dir / f"{task_id}.log"
            log_file = log_path.open("w", encoding="utf-8")
            os.chmod(log_path, 0o600)
            self.process = subprocess.Popen(
                command, cwd=PROJECT_DIR, stdout=log_file, stderr=subprocess.STDOUT,
                text=True, start_new_session=True,
            )
            log_file.close()
            self.task = {
                "id": task_id, "kind": kind, "subject": subject, "pid": self.process.pid,
                "running": True, "exit_code": None, "log_path": str(log_path), "batch_id": batch_id,
                "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            started, _ = self._process_identity(self.process.pid)
            self.task["process_started_ticks"] = started
            self._save()
            return dict(self.task)


MANAGER = TaskManager()
_DOCTOR_LOCK = threading.Lock()
_DOCTOR_CACHE: tuple[float, list[dict]] = (0.0, [])


def doctor() -> list[dict]:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, *, blocking: bool = True, warning: bool = False) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "blocking": blocking, "warning": warning})

    runtime = PRIVATE_DIR / "runtime.env"
    add("私有配置", runtime.is_file(), "private/runtime.env 已找到" if runtime.is_file() else "缺少 private/runtime.env")
    if runtime.is_file():
        add("配置权限", not bool(runtime.stat().st_mode & 0o077), "权限为 0600" if not runtime.stat().st_mode & 0o077 else "权限必须改为 0600")
        values = {}
        for line in runtime.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
        required = [
            "YESCAPTCHA_API_KEY", "IMAP_SERVER", "IMAP_PASSWORD", "MAILU_DOMAIN",
            "MAILU_DB", "MAILU_ADMIN_CONTAINER", "MAILU_FLASK_BIN",
            "MAILU_IMAP_CONTAINER", "MAILU_MAIL_ROOT", "SUB2API_ENV",
            "SUB2API_URL", "SUB2API_GROUP", "SUB2API_POSTGRES_CONTAINER",
            "SUB2API_PG_USER", "SUB2API_PG_DB", "SUB2API_IMPORT_TOOL",
            "GROK_ACCOUNT_BASE_URL",
        ]
        missing = [key for key in required if not values.get(key)]
        add("必要字段", not missing, "字段完整" if not missing else "缺少: " + ", ".join(missing))
        try:
            max_workers = int(values.get("GROK_MAX_REGISTRATION_WORKERS", "2") or "2")
            workers_ok = 1 <= max_workers <= 16
        except ValueError:
            max_workers = 0
            workers_ok = False
        add("注册并发", workers_ok, f"最多 {max_workers} 路协议注册" if workers_ok else "GROK_MAX_REGISTRATION_WORKERS 必须为 1 到 16")
        try:
            pool = load_proxy_pool(values.get("GROK_PROXY_POOL_FILE", ""), values)
            if pool.configured:
                detail = f"已启用 {pool.enabled_count} 个节点，可用租约 {pool.capacity}"
            elif values.get("HTTPS_PROXY") or values.get("HTTP_PROXY"):
                detail = "未配置节点池，使用现有单一 sticky 代理"
            else:
                detail = "未配置代理，注册任务直连"
            add("注册代理池", True, detail, blocking=False)
        except ProxyPoolError as exc:
            add("注册代理池", False, str(exc))
        edge = next((Path(value) for value in (
            "/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable",
            "/usr/bin/microsoft-edge-dev",
        ) if Path(value).is_file()), None)
        try:
            import playwright.sync_api  # noqa: F401
            playwright_ok = True
        except ImportError:
            playwright_ok = False
        add(
            "浏览器注册",
            bool(edge and playwright_ok),
            f"Playwright + {edge}" if edge and playwright_ok else "需要 Playwright Python 和 Microsoft Edge",
            blocking=False,
        )
        parsed = urlparse(values.get("SUB2API_URL", ""))
        add("Sub2API 地址", parsed.hostname in {"127.0.0.1", "localhost", "::1"}, "本机回环地址，不使用外部代理" if parsed.hostname in {"127.0.0.1", "localhost", "::1"} else "必须使用本机回环地址")
        helper = Path(values.get("SUB2API_IMPORT_TOOL", ""))
        add("导入工具", helper.is_file(), str(helper) if helper.is_file() else "导入工具路径无效")
        grok_target_ok = (
            values.get("SUB2API_GROUP") == "grok"
            and values.get("GROK_ACCOUNT_BASE_URL", "").rstrip("/") == "http://grok-cli-proxy:8080/v1"
        )
        add(
            "Grok 导入目标", grok_target_ok,
            "grok 组 → Docker 内网 CLI 头代理" if grok_target_ok else "必须配置 grok 组和 http://grok-cli-proxy:8080/v1",
        )
        mailu_db = Path(values.get("MAILU_DB", ""))
        add("Mailu 数据库", mailu_db.is_file(), str(mailu_db) if mailu_db.is_file() else "Mailu 数据库路径无效")
        sub2api_env = Path(values.get("SUB2API_ENV", ""))
        add("Sub2API 配置", sub2api_env.is_file(), str(sub2api_env) if sub2api_env.is_file() else "Sub2API 环境文件无效")
        for label, key in (
            ("Mailu Admin", "MAILU_ADMIN_CONTAINER"),
            ("Mailu IMAP", "MAILU_IMAP_CONTAINER"),
            ("PostgreSQL", "SUB2API_POSTGRES_CONTAINER"),
        ):
            container = values.get(key, "")
            proc = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", container], text=True, capture_output=True) if container else None
            ok = bool(proc and proc.returncode == 0 and proc.stdout.strip() == "true")
            add(label, ok, f"容器 {container} 正常" if ok else f"容器 {container or '(未配置)'} 不可用")
        proxy = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{end}}", "sub2api-prod-grok-cli-proxy"],
            text=True, capture_output=True,
        )
        proxy_ok = proxy.returncode == 0 and proxy.stdout.strip() == "true healthy"
        add("Grok CLI 头代理", proxy_ok, "容器 healthy" if proxy_ok else "sub2api-prod-grok-cli-proxy 不健康")
        override = subprocess.run(
            ["docker", "exec", "sub2api-prod", "sh", "-lc", "test \"$XAI_ALLOW_UNSAFE_URL_OVERRIDES\" = true"],
            capture_output=True,
        )
        add("内网上游许可", override.returncode == 0, "Sub2API 允许受控内网 Grok 上游" if override.returncode == 0 else "XAI_ALLOW_UNSAFE_URL_OVERRIDES 未启用")
        connectivity = subprocess.run(
            ["docker", "exec", "sub2api-prod", "wget", "-q", "-T", "5", "-O", "/dev/null", "http://grok-cli-proxy:8080/healthz"],
            capture_output=True,
        )
        add("容器内连通性", connectivity.returncode == 0, "Sub2API 可访问 Grok CLI 头代理" if connectivity.returncode == 0 else "Sub2API 无法访问 grok-cli-proxy")
        pg_container = values.get("SUB2API_POSTGRES_CONTAINER", "")
        pg_user = values.get("SUB2API_PG_USER", "")
        pg_db = values.get("SUB2API_PG_DB", "")
        group_check = subprocess.run([
            "docker", "exec", pg_container, "psql", "-U", pg_user, "-d", pg_db, "-Atc",
            "select count(*) from groups where deleted_at is null and name='grok' and platform='grok' and status='active' and is_exclusive and require_oauth_only;",
        ], text=True, capture_output=True) if pg_container and pg_user and pg_db else None
        group_ok = bool(group_check and group_check.returncode == 0 and group_check.stdout.strip() == "1")
        add("Grok 生产分组", group_ok, "独占且仅 OAuth 的 grok 分组已就绪" if group_ok else "grok 生产分组缺失或配置不正确")
        admin = values.get("MAILU_ADMIN_CONTAINER", "")
        flask_bin = values.get("MAILU_FLASK_BIN", "")
        proc = subprocess.run(["docker", "exec", admin, "test", "-x", flask_bin], capture_output=True) if admin and flask_bin else None
        add("Mailu CLI", bool(proc and proc.returncode == 0), flask_bin if proc and proc.returncode == 0 else "Mailu Flask CLI 不可执行")
        wooai_config = Path("/etc/nginx/sites-available/wooai")
        config_text = wooai_config.read_text(encoding="utf-8", errors="replace") if wooai_config.is_file() else ""
        configured_8787 = "127.0.0.1:8787" in config_text
        with socket.socket() as probe:
            probe.settimeout(0.3)
            port_8787_up = probe.connect_ex(("127.0.0.1", 8787)) == 0
        if configured_8787 and not port_8787_up:
            add(
                "Responses 网关", False,
                "主上游 127.0.0.1:8787 未监听；当前会回退 13080，但会产生连接拒绝日志",
                blocking=False, warning=True,
            )
        else:
            add("Responses 网关", True, "Responses 主上游可用或未配置失效端口")
    return checks


def cached_doctor(*, force: bool = False) -> list[dict]:
    global _DOCTOR_CACHE
    with _DOCTOR_LOCK:
        cached_at, checks = _DOCTOR_CACHE
        if not force and checks and time.monotonic() - cached_at < DOCTOR_CACHE_TTL:
            return [dict(item) for item in checks]
        checks = doctor()
        _DOCTOR_CACHE = (time.monotonic(), checks)
        return [dict(item) for item in checks]


def state_payload() -> dict:
    task = MANAGER.current()
    batches = list_batches()
    active = [item for item in batches if item.get("status") in ACTIVE_BATCH_STATUSES]
    if active:
        batch = active[0]
        if task and task.get("running"):
            batch["runtime_state"] = "running"
        elif task and task.get("interrupted"):
            batch["runtime_state"] = "interrupted"
            batch["action_hint"] = "任务进程已中断。检查日志后重新开始；已有 auth 的导入失败批次可直接续跑。"
        else:
            batch["runtime_state"] = "interrupted"
            batch["action_hint"] = "没有检测到对应执行进程。请检查日志和批次产物后再决定是否重试。"
    return {"task": task, "batches": batches, "checks": cached_doctor()}


class Handler(BaseHTTPRequestHandler):
    server_version = "GrokBatchConsole/1.0"

    def log_message(self, fmt: str, *args) -> None:
        return

    def send_json(self, payload: object, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 65536:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def serve_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/":
            return self.serve_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        if self.path == "/app.js":
            return self.serve_file(WEB_DIR / "app.js", "text/javascript; charset=utf-8")
        if self.path == "/styles.css":
            return self.serve_file(WEB_DIR / "styles.css", "text/css; charset=utf-8")
        if self.path == "/api/state":
            return self.send_json(state_payload())
        if self.path.startswith("/api/task-log"):
            task = MANAGER.current()
            log = Path(task["log_path"]).read_text(encoding="utf-8", errors="replace")[-30000:] if task and Path(task["log_path"]).is_file() else ""
            return self.send_json({"log": log})
        if self.path.startswith("/api/batches/"):
            batch_id = safe_batch_id(self.path.split("/")[3])
            return self.send_json(batch_summary(RUNS_DIR / batch_id))
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        try:
            payload = self.read_body()
            if self.path == "/api/start":
                count = int(payload.get("count", 1))
                if count < 1 or count > 100:
                    raise ValueError("注册数量必须在 1 到 100 之间")
                workers = int(payload.get("workers", 2))
                if workers < 1 or workers > 16:
                    raise ValueError("并发数必须在 1 到 16 之间")
                registration_backend = str(payload.get("registration_backend") or "protocol-yescaptcha")
                if registration_backend not in {"protocol-yescaptcha", "browser-playwright-edge"}:
                    raise ValueError("未知注册方式")
                if registration_backend == "browser-playwright-edge" and workers != 1:
                    raise ValueError("浏览器注册必须使用 1 路并发")
                command = [
                    "python3", str(PROJECT_DIR / "scripts/register_and_import.py"),
                    "--count", str(count), "--workers", str(workers),
                    "--registration-backend", registration_backend,
                    "--confirm-production-write",
                ]
                if payload.get("import_partial"):
                    command.append("--import-partial")
                if payload.get("cleanup_failed_mailboxes"):
                    command.append("--cleanup-failed-mailboxes")
                label = "浏览器" if registration_backend == "browser-playwright-edge" else "协议"
                task = MANAGER.start(command, "new-batch", f"{label}注册并导入 {count} 个账号（{workers} 路）")
                return self.send_json(task, HTTPStatus.ACCEPTED)
            if self.path.startswith("/api/resume/"):
                batch_id = safe_batch_id(self.path.split("/")[3])
                manifest = batch_summary(RUNS_DIR / batch_id)
                if not manifest.get("resumable"):
                    raise ValueError("该批次当前不可续跑")
                command = [
                    "python3", str(PROJECT_DIR / "scripts/register_and_import.py"),
                    "--resume", batch_id, "--confirm-production-write",
                ]
                if manifest.get("failed_registrations"):
                    command.append("--import-partial")
                task = MANAGER.start(command, "resume-import", f"继续导入 {batch_id}", batch_id=batch_id)
                return self.send_json(task, HTTPStatus.ACCEPTED)
            if self.path == "/api/doctor":
                return self.send_json({"checks": cached_doctor(force=True)})
            self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def main() -> None:
    parser = argparse.ArgumentParser(description="Grok batch registration web console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=17860)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Refusing non-loopback bind; use a local reverse proxy with authentication if needed")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Grok Batch Console: http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
