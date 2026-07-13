#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Grok 注册机 - TTK GUI 版本
整合 DrissionPage_example.py, openai_register.py, batch_open_nsfw.py
"""

import threading
import datetime
import time
import os
import sys
import argparse
import gc
import secrets
import struct
import random
import re
import string
import json

from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import PageDisconnectedError
from curl_cffi import requests
import psutil


CONFIG_FILE = os.environ.get(
    "GROK_CLIENT_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
)
MEMORY_CLEANUP_INTERVAL = 5

DEFAULT_CONFIG = {
    "duckmail_api_key": "",
    "cloudflare_api_base": "",
    "cloudflare_api_key": "",
    "cloudflare_auth_mode": "none",
    "cloudflare_path_domains": "/api/domains",
    "cloudflare_path_accounts": "/api/new_address",
    "cloudflare_path_token": "/api/token",
    "cloudflare_path_messages": "/api/mails",
    "proxy": "http://127.0.0.1:7890",
    "enable_nsfw": True,
    "register_count": 1,
    "max_concurrency": 1,
    "target_successes": 0,
    "accounts_output_dir": "",
    "mail_credentials_file": "",
    "success_records_file": "",
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    # ===== Sub2API auth 导出 / 免费 Grok 4.5（OIDC，非 Web SSO）=====
    # 注册成功后走设备码 OIDC 铸造 token，写出 Sub2API 的 xai-<email>.json，并可
    # 推送到远端 Sub2API bridge 导入。免费号用 cli-chat-proxy，Sub2API 请求 grok 时
    # 自带 x-grok-client-version 头，不会 426。SSO cookie 不能替代 OIDC。
    "cpa_export_enabled": True,          # 注册成功后是否铸造 OIDC 并写 Sub2API auth
    "cpa_auth_dir": "./cpa_auths",       # 本地写盘目录（xai-<email>.json）
    "cpa_base_url": "https://cli-chat-proxy.grok.com/v1",  # 写进 auth 的 base_url；付费用 https://api.x.ai/v1
    # 远端推送到 Sub2API bridge（POST /v0/management/auth-files）
    "cpa_push_enabled": False,           # 是否推送到远端 Sub2API bridge
    "cpa_remote_base": "",               # 远端 Sub2API bridge 地址
    "cpa_remote_secret": "",             # 远端 Sub2API bridge 管理密钥
    "cpa_remote_verify_tls": True,       # https 远端是否校验证书
    "cpa_push_proxy": "",                # 推送用代理；空=直连（不走 mint 代理）
    "cpa_push_required": True,           # 推送失败不得计为完整成功
    "cpa_require_probe_passed": True,    # bridge 必须返回 probe=passed
    "cpa_require_created": False,        # 新注册批次可要求 bridge action=created
    # OIDC 设备码铸造（独立 Chromium）
    "mint_proxy": "",                    # 铸造专用代理；空=复用 proxy
    "mint_timeout_sec": 300,             # 单账号铸造超时（秒）
    "mint_required": True,               # OAuth 铸造失败不得计为完整成功
    # 以下优化默认关闭 = 与最初脚本一致；实测三者都不影响 Turnstile 通过，可按需开启。
    # （注：Blink 层禁图会导致验证过不去，已弃用；省图片带宽改由 block_media_fonts 承担）
    "block_media_fonts": False,  # 网络层拦截图片/字体/媒体省带宽（省带宽主力，不影响验证）
    "hide_window": True,         # 使用 SW_HIDE 隐藏任务浏览器，不抢占前台
    "stealth_patch": False,      # 内联 turnstilePatch 全局注入
}

config = DEFAULT_CONFIG.copy()
_cf_domain_index = 0
_cf_domain_lock = threading.Lock()


class RegistrationCancelled(Exception):
    pass


class AccountRetryNeeded(Exception):
    pass


def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            config = {**DEFAULT_CONFIG, **loaded}
        except Exception:
            config = DEFAULT_CONFIG.copy()
    return config


def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"保存配置失败: {e}")


def ensure_stable_python_runtime():
    if sys.version_info < (3, 14) or os.environ.get("DPE_REEXEC_DONE") == "1":
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app_data, "Programs", "Python", "Python313", "python.exe"),
    ]

    current_python = os.path.normcase(os.path.abspath(sys.executable))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        if os.path.normcase(os.path.abspath(candidate)) == current_python:
            return

        print(
            f"[*] 检测到 Python {sys.version.split()[0]}，自动切换到更稳定的解释器: {candidate}"
        )
        env = os.environ.copy()
        env["DPE_REEXEC_DONE"] = "1"
        os.execve(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]], env)


def warn_runtime_compatibility():
    if sys.version_info >= (3, 14):
        print(
            "[提示] 当前 Python 为 3.14+；若出现 Mail.tm TLS 异常，建议改用 Python 3.12 或 3.13。"
        )


ensure_stable_python_runtime()
warn_runtime_compatibility()

load_config()

EXTENSION_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "turnstilePatch")
)


DUCKMAIL_API_BASE = "https://api.duckmail.sbs"


def get_proxies():
    # bridge HTTP 调用默认直连，浏览器通过 --proxy-server 单独设置。
    return {}


def get_duckmail_api_key():
    return config.get("duckmail_api_key", "")


def get_cloudflare_api_base():
    return str(config.get("cloudflare_api_base", "") or "").rstrip("/")


def get_cloudflare_api_key():
    return config.get("cloudflare_api_key", "")


def get_cloudflare_auth_mode():
    return str(config.get("cloudflare_auth_mode", "none") or "none").lower()


def get_cloudflare_path(key, default_path):
    raw = str(config.get(key, default_path) or default_path).strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw


def cloudflare_build_headers(content_type=False):
    headers = {"Content-Type": "application/json"} if content_type else {}
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key:
        if mode == "x-api-key":
            headers["X-API-Key"] = key
        elif mode == "x-admin-auth":
            headers["x-admin-auth"] = key
        elif mode != "none":
            headers["Authorization"] = f"Bearer {key}"
    return headers


def cloudflare_apply_auth_params(params=None):
    merged = dict(params or {})
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key and mode == "query-key":
        merged["key"] = key
    return merged


def cloudflare_next_default_domain():
    """按配置轮换选择 Cloudflare 临时邮箱域名。"""
    global _cf_domain_index
    domains = [x.strip() for x in str(config.get("defaultDomains", "") or "").split(",") if x.strip()]
    if not domains:
        return ""
    with _cf_domain_lock:
        domain = domains[_cf_domain_index % len(domains)]
        _cf_domain_index += 1
    return domain


def cloudflare_is_admin_create_path(path):
    """判断当前创建邮箱路径是否为 cloudflare_temp_email 管理员创建接口。"""
    return str(path or "").rstrip("/").lower() == "/admin/new_address"


def _pick_list_payload(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            return data.get("results")
        if isinstance(data.get("hydra:member"), list):
            return data.get("hydra:member")
        if isinstance(data.get("data"), list):
            return data.get("data")
        if isinstance(data.get("messages"), list):
            return data.get("messages")
        if isinstance(data.get("data"), dict):
            nested = data.get("data")
            if isinstance(nested.get("messages"), list):
                return nested.get("messages")
    return []


def cloudflare_create_temp_address(api_base):
    """适配 cloudflare_temp_email 新建地址接口并兼容 admin 创建模式。"""
    path = get_cloudflare_path("cloudflare_path_accounts", "/api/new_address")
    url = f"{api_base}{path}"
    domain = cloudflare_next_default_domain()
    is_admin_create = cloudflare_is_admin_create_path(path)
    if is_admin_create:
        payload = {"name": generate_username(10), "enablePrefix": True}
        if domain:
            payload["domain"] = domain
        headers = cloudflare_build_headers(content_type=True)
    else:
        payload = {}
        if domain:
            payload["domain"] = domain
        headers = {"Content-Type": "application/json"}
    resp = http_post(url, json=payload, headers=headers)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare {path} 返回非JSON: {resp.text[:300]}")
    address = data.get("address")
    jwt = data.get("jwt")
    if not address or not jwt:
        raise Exception(f"Cloudflare {path} 缺少 address/jwt: {data}")
    return address, jwt


def get_user_agent():
    return config.get(
        "user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    )


def export_cpa_after_register(email, password, session=None, sso="", log_callback=None):
    """注册成功后铸造 Grok Build OIDC，写出 Sub2API auth 的 xai-<email>.json，
    并按配置推送到远端 Sub2API bridge 导入。

    走独立 Chromium 完成设备码确认，再轮询 token；本地写到 config['cpa_auth_dir']，
    远端推送到 config['cpa_remote_base'] 的 /v0/management/auth-files。
    SSO cookie 只用于尽量跳过二次登录，不能替代 OIDC。
    """
    log = log_callback or cli_log
    if not config.get("cpa_export_enabled", True):
        log("[cpa] 已关闭导出，跳过")
        return {"ok": False, "skipped": True, "reason": "disabled"}

    try:
        import cpa_export
    except Exception as exc:
        log(f"[cpa] 导入 cpa_export 失败: {exc}")
        if config.get("mint_required", False):
            raise
        return {"ok": False, "error": f"import: {exc}"}

    # 铸造用独立浏览器，只借注册页导出 cookie 以尽量跳过二次登录
    page = getattr(session, "page", None) if session is not None else None
    try:
        result = cpa_export.export_cpa_for_account(
            email,
            password,
            page=page,
            config=config,
            log_callback=log,
        )
    except Exception as exc:
        log(f"[cpa] 导出异常: {exc}")
        if config.get("mint_required", False):
            raise
        return {"ok": False, "error": str(exc)}

    if result.get("ok"):
        tail = " 并已推送远端" if result.get("pushed") else ""
        log(f"[+] Sub2API auth 已写出: {result.get('path')}{tail}")
    elif result.get("skipped"):
        log(f"[cpa] 跳过: {result.get('reason')}")
    else:
        log(f"[!] Sub2API auth 导出未成功: {result.get('error') or result}")
    return result


def cleanup_mint_browsers():
    """关闭铸造 worker 复用的独立 Chromium（线程退出时调用）。"""
    try:
        from oidc_mint import shutdown_mint_browsers
        shutdown_mint_browsers()
    except Exception:
        pass


def _kill_proc_tree(pid, timeout=3):
    """强杀指定进程及其所有子进程（浏览器 quit 不干净时兜底）。"""
    if not pid:
        return
    try:
        parent = psutil.Process(pid)
    except Exception:
        return
    procs = []
    try:
        procs = parent.children(recursive=True)
    except Exception:
        pass
    procs.append(parent)
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass
    try:
        psutil.wait_procs(procs, timeout=timeout)
    except Exception:
        pass


def _kill_chrome_session(pid=None, port=None):
    """精准杀掉某个浏览器会话：优先按调试端口定位真正的 chrome 主进程，再连启动器 pid 一起杀进程树。"""
    targets = set()
    if port:
        marker = f"--remote-debugging-port={port}"
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                if (p.info.get("name") or "").lower() == "chrome.exe" and marker in " ".join(p.info.get("cmdline") or []):
                    targets.add(p.pid)
            except Exception:
                continue
    if pid:
        targets.add(pid)
    for tp in targets:
        _kill_proc_tree(tp)


def _hide_chrome_session_windows(pid=None, port=None):
    """Hide top-level windows owned by this task's Chromium session on Windows."""
    if os.name != "nt":
        return 0

    targets = set()
    if port:
        marker = f"--remote-debugging-port={port}"
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                if (proc.info.get("name") or "").lower() != "chrome.exe":
                    continue
                if marker not in " ".join(proc.info.get("cmdline") or []):
                    continue
                targets.add(proc.pid)
                targets.update(child.pid for child in proc.children(recursive=True))
            except Exception:
                continue
    if pid:
        try:
            proc = psutil.Process(pid)
            targets.add(proc.pid)
            targets.update(child.pid for child in proc.children(recursive=True))
        except Exception:
            targets.add(pid)
    if not targets:
        return 0

    try:
        import ctypes
        from ctypes import wintypes

        hidden = [0]
        user32 = ctypes.windll.user32

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def enum_window(hwnd, _lparam):
            owner_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
            if owner_pid.value in targets and user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, 0)  # SW_HIDE
                hidden[0] += 1
            return True

        user32.EnumWindows(enum_window, 0)
        return hidden[0]
    except Exception:
        return 0


def cleanup_stray_chrome(log_callback=None, rounds=4):
    """不做全局进程扫描。

    BrowserSession.stop() 只按本任务记录的 PID/调试端口回收自己启动的浏览器。
    未能证明归属的 Chrome、Edge 和更新进程必须保留。
    """
    del rounds
    (log_callback or cli_log)("[Debug] 已跳过全局浏览器进程清理")


def create_browser_options():
    options = ChromiumOptions()
    # Linux: 使用 Microsoft Edge 替代 Chrome
    import platform
    is_linux = platform.system() == "Linux"
    if is_linux:
        edge_paths = ["/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable"]
        for p in edge_paths:
            import os as _os
            if _os.path.isfile(p):
                options.set_browser_path(p)
                break
    # Linux: 固定端口避免 auto_port 与 Chromium 初始化冲突；加 --headless=new 以在无头环境运行
    import socket as _socket
    _s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    _s.bind(('', 0))
    _free_port = _s.getsockname()[1]
    _s.close()
    options.set_local_port(_free_port)
    options.set_timeouts(base=1)
    # Linux: 使用 Xvfb 虚拟显示，不设 headless 以避免 Cloudflare 检测
    # 关键：全新临时用户目录下，Chrome 138 会走「首次启动」流程，DrissionPage 取不到
    # 标签页而在 Chromium() 启动处卡死。--no-first-run 跳过首启，--no-default-browser-check
    # 屏蔽「设为默认浏览器」提示；二者都不影响 Turnstile 渲染。
    options.set_argument("--no-first-run")
    options.set_argument("--no-default-browser-check")
    # Linux root 环境必需
    options.set_argument("--no-sandbox")
    options.set_argument("--disable-dev-shm-usage")
    options.set_argument("--disable-gpu")
    options.set_argument("--window-size=1280,900")

    # 禁用后台网络（含更新检查），减少 Chrome 拉起 GoogleUpdate.exe 残留
    options.set_argument("--disable-background-networking")
    options.set_argument("--disable-component-update")
    # 禁用后台标签/窗口节流，避免多窗口时后台浏览器被降频、响应变慢
    options.set_argument("--disable-background-timer-throttling")
    options.set_argument("--disable-backgrounding-occluded-windows")
    options.set_argument("--disable-renderer-backgrounding")
    # 代理：注册必须走代理换 IP；Chrome 需显式设置 --proxy-server
    proxy = config.get("proxy", "")
    if proxy:
        options.set_argument(f"--proxy-server={proxy}")
    # Windows 隐藏任务浏览器；Linux/Xvfb 本身不可见，最小化会改变页面可见性。
    if config.get("hide_window", False) and not is_linux:
        options.set_argument("--window-position=-32000,-32000")
        options.set_argument("--start-minimized")
    # 加载 turnstilePatch 扩展（伪造自动化鼠标事件指纹，帮过 Turnstile）
    if os.path.exists(EXTENSION_PATH):
        options.add_extension(EXTENSION_PATH)
    return options


def _build_request_kwargs(**kwargs):
    request_kwargs = dict(kwargs)
    proxies = request_kwargs.pop("proxies", None)
    if proxies is None:
        proxies = get_proxies()
    if proxies:
        request_kwargs["proxies"] = proxies
    request_kwargs.setdefault("timeout", 15)
    return request_kwargs


def http_get(url, **kwargs):
    try:
        return requests.get(url, **_build_request_kwargs(**kwargs))
    except Exception as exc:
        err = str(exc)
        # 代理不可用时自动回退为直连，避免整个流程直接失败
        if "127.0.0.1 port 7890" in err or "Could not connect to server" in err:
            retry_kwargs = dict(kwargs)
            retry_kwargs["proxies"] = {}
            return requests.get(url, **_build_request_kwargs(**retry_kwargs))
        raise


def http_post(url, **kwargs):
    try:
        return requests.post(url, **_build_request_kwargs(**kwargs))
    except Exception as exc:
        err = str(exc)
        if "127.0.0.1 port 7890" in err or "Could not connect to server" in err:
            retry_kwargs = dict(kwargs)
            retry_kwargs["proxies"] = {}
            return requests.post(url, **_build_request_kwargs(**retry_kwargs))
        raise


def raise_if_cancelled(cancel_callback=None):
    if cancel_callback and cancel_callback():
        raise RegistrationCancelled("鐢ㄦ埛鍋滄娉ㄥ唽")


def sleep_with_cancel(seconds, cancel_callback=None):
    deadline = time.time() + max(seconds, 0)
    while True:
        raise_if_cancelled(cancel_callback)
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def get_domains(api_key=None):
    headers = {}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = http_get(f"{DUCKMAIL_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def create_account(address, password, api_key=None, expires_in=0):
    headers = {"Content-Type": "application/json"}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = {"address": address, "password": password, "expiresIn": expires_in}
    resp = http_post(f"{DUCKMAIL_API_BASE}/accounts", json=data, headers=headers)
    resp.raise_for_status()
    return resp.json()


def get_token(address, password):
    data = {"address": address, "password": password}
    resp = http_post(f"{DUCKMAIL_API_BASE}/token", json=data)
    resp.raise_for_status()
    return resp.json().get("token")


def get_messages(token):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def get_message_detail(token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    return resp.json()


def cloudflare_get_domains(api_base, api_key=None):
    headers = cloudflare_build_headers(content_type=False)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_domains", "/domains")
    params = cloudflare_apply_auth_params()
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    return _pick_list_payload(resp.json())


def cloudflare_create_account(api_base, address, password, api_key=None, expires_in=0):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    payload = {"address": address, "password": password, "expiresIn": expires_in}
    path = get_cloudflare_path("cloudflare_path_accounts", "/accounts")
    params = cloudflare_apply_auth_params()
    resp = http_post(f"{api_base}{path}", json=payload, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def cloudflare_get_token(api_base, address, password, api_key=None):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_token", "/token")
    resp = http_post(
        f"{api_base}{path}",
        json={"address": address, "password": password},
        headers=headers,
        params=cloudflare_apply_auth_params(),
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        if data.get("token"):
            return data.get("token")
        if isinstance(data.get("data"), dict) and data["data"].get("token"):
            return data["data"].get("token")
    return None


def cloudflare_get_messages(api_base, token):
    headers = {"Authorization": f"Bearer {token}"}
    path = get_cloudflare_path("cloudflare_path_messages", "/messages")
    params = {"limit": 20, "offset": 0}
    params = cloudflare_apply_auth_params(params)
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare messages 返回非JSON: {resp.text[:300]}")
    return _pick_list_payload(data)


def cloudflare_get_message_detail(api_base, token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    candidates = [
        f"{api_base}/api/mail/{message_id}",
        f"{api_base}{get_cloudflare_path('cloudflare_path_messages', '/messages')}/{message_id}",
    ]
    last_err = None
    for url in candidates:
        try:
            resp = http_get(
                url,
                headers=headers,
                params=cloudflare_apply_auth_params(),
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                return data["data"]
            return data
        except Exception as exc:
            last_err = exc
            continue
    raise Exception(f"Cloudflare 获取邮件详情失败: {last_err}")


YYDS_API_BASE = "https://maliapi.215.im/v1"


def get_yyds_api_key():
    return config.get("yyds_api_key", "")


def get_yyds_jwt():
    return config.get("yyds_jwt", "")


def yyds_get_domains(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", []) if data.get("success") else []


def yyds_create_account(address=None, domain=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    payload = {}
    if address:
        payload["address"] = address
    if domain:
        payload["domain"] = domain
    elif key or token:
        payload["autoDomainStrategy"] = "prefer_owned"
    resp = http_post(f"{YYDS_API_BASE}/accounts", json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 鍒涘缓閭澶辫触: {data}")


def yyds_get_token(address, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_post(
        f"{YYDS_API_BASE}/token", json={"address": address}, headers=headers
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("token")
    raise Exception(f"YYDS 鑾峰彇token澶辫触: {data}")


def yyds_get_messages(address, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(
        f"{YYDS_API_BASE}/messages",
        params={"address": address},
        headers=headers,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("messages", [])
    return []


def yyds_get_message_detail(message_id, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 鑾峰彇閭欢璇︽儏澶辫触: {data}")


def yyds_generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def yyds_pick_domain(api_key=None, jwt=None):
    domains = yyds_get_domains(api_key=api_key, jwt=jwt)
    if not domains:
        raise Exception("YYDS 娌℃湁杩斿洖浠讳綍鍙敤鍩熷悕")
    private = [d for d in domains if d.get("isVerified") and not d.get("isPublic")]
    if private:
        return private[0]["domain"]
    public = [d for d in domains if d.get("isVerified") and d.get("isPublic")]
    if public:
        return public[0]["domain"]
    verified = [d for d in domains if d.get("isVerified")]
    if verified:
        return verified[0]["domain"]
    raise Exception("YYDS 鏃犲凡楠岃瘉鍩熷悕鍙敤")


def yyds_get_email_and_token(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    if not token and not key:
        raise Exception("YYDS API Key 或 JWT 未配置")
    domain = yyds_pick_domain(api_key=key, jwt=token)
    username = yyds_generate_username(10)
    result = yyds_create_account(
        address=username, domain=domain, api_key=key, jwt=token
    )
    address = result.get("address") or f"{username}@{domain}"
    temp_token = result.get("token")
    if not temp_token:
        temp_token = yyds_get_token(address, api_key=key, jwt=token)
    if not temp_token:
        raise Exception("鑾峰彇 YYDS token 澶辫触")
    print(f"[*] 宸插垱寤?YYDS 閭: {address}")
    return address, temp_token


def yyds_get_oai_code(
    token,
    address,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    jwt=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = yyds_get_messages(address, token=token, jwt=jwt)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] YYDS 鎷夊彇閭欢鍒楄〃澶辫触: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            to_addrs = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if address.lower() not in to_addrs:
                continue
            try:
                detail = yyds_get_message_detail(msg_id, token=token, jwt=jwt)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] YYDS 鑾峰彇閭欢璇︽儏澶辫触: {exc}")
                continue
            parts = []
            text_body = detail.get("text") or ""
            if text_body:
                parts.append(text_body)
            html_list = detail.get("html") or []
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            combined = "\n".join(parts)
            subject = detail.get("subject", "")
            if log_callback:
                log_callback(f"[Debug] YYDS 鏀跺埌閭欢: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[Debug] YYDS 浠庨偖浠朵腑鎻愬彇鍒伴獙璇佺爜: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"YYDS 在 {timeout}s 内未收到验证码邮件")


def generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def pick_domain(api_key=None):
    domains = get_domains(api_key=api_key)
    if not domains:
        raise Exception("DuckMail 娌℃湁杩斿洖浠讳綍鍙敤鍩熷悕")
    private = [d for d in domains if d.get("ownerId")]
    verified_private = [d for d in private if d.get("isVerified")]
    if verified_private:
        return verified_private[0]["domain"]
    public = [d for d in domains if d.get("isVerified")]
    if public:
        return public[0]["domain"]
    raise Exception("DuckMail 鏃犲凡楠岃瘉鍩熷悕鍙敤")


def get_email_provider():
    return config.get("email_provider", "duckmail")


def get_email_and_token(api_key=None):
    provider = get_email_provider()
    if provider == "yyds":
        return yyds_get_email_and_token(api_key=api_key, jwt=get_yyds_jwt())
    if provider == "cloudflare":
        api_base = get_cloudflare_api_base()
        if not api_base:
            raise Exception("Cloudflare API Base 未配置")
        try:
            # cloudflare_temp_email 专用模式
            return cloudflare_create_temp_address(api_base)
        except Exception as primary_exc:
            # 兜底回退到 Mail.tm 风格
            key = api_key or get_cloudflare_api_key()
            domains = cloudflare_get_domains(api_base, api_key=key)
            if not domains:
                raise Exception(f"Cloudflare 创建邮箱失败: {primary_exc}")
            verified = [d for d in domains if d.get("isVerified")]
            target = verified[0] if verified else domains[0]
            domain = target.get("domain")
            if not domain:
                raise Exception("Cloudflare 域名数据格式错误，缺少 domain 字段")
            username = generate_username(10)
            address = f"{username}@{domain}"
            password = secrets.token_urlsafe(12)
            cloudflare_create_account(
                api_base, address, password, api_key=key, expires_in=0
            )
            token = cloudflare_get_token(api_base, address, password, api_key=key)
            if not token:
                raise Exception("获取 Cloudflare 邮箱 token 失败")
            return address, token
    key = api_key or get_duckmail_api_key()
    domain = pick_domain(api_key=key)
    username = generate_username(10)
    address = f"{username}@{domain}"
    password = secrets.token_urlsafe(12)
    create_account(address, password, api_key=key, expires_in=0)
    token = get_token(address, password)
    if not token:
        raise Exception("鑾峰彇 DuckMail token 澶辫触")
    return address, token


def get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    provider = get_email_provider()
    if provider == "yyds":
        return yyds_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            jwt=get_yyds_jwt(),
            cancel_callback=cancel_callback,
        )
    if provider == "cloudflare":
        return cloudflare_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    return duckmail_get_oai_code(
        dev_token,
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )


def extract_verification_code(text, subject=""):
    if subject:
        match = re.search(r"^([A-Z0-9]{3}-[A-Z0-9]{3})\s+xAI", subject, re.IGNORECASE)
        if match:
            return match.group(1)
    match = re.search(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", text, re.IGNORECASE)
    if match:
        return match.group(1)
    patterns = [
        r"verification\s+code[:\s]+(\d{4,8})",
        r"your\s+code[:\s]+(\d{4,8})",
        r"confirm(?:ation)?\s+code[:\s]+(\d{4,8})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def duckmail_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = get_messages(dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 鎷夊彇閭欢鍒楄〃澶辫触: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            to_raw = msg.get("to")
            recipients = []
            if isinstance(to_raw, str):
                recipients.append(to_raw.lower())
            elif isinstance(to_raw, list):
                for t in to_raw:
                    if isinstance(t, str):
                        recipients.append(t.lower())
                    elif isinstance(t, dict):
                        recipients.append(t.get("address", "").lower())
            if email.lower() not in recipients:
                continue
            try:
                detail = get_message_detail(dev_token, msg_id)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 鑾峰彇閭欢璇︽儏澶辫触: {exc}")
                continue
            parts = []
            text_body = detail.get("text") or ""
            if text_body:
                parts.append(text_body)
            html_list = detail.get("html") or []
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            combined = "\n".join(parts)
            subject = detail.get("subject", "")
            if log_callback:
                log_callback(f"[Debug] 鏀跺埌閭欢: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[Debug] 浠庨偖浠朵腑鎻愬彇鍒伴獙璇佺爜: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"在 {timeout}s 内未收到验证码邮件")


def cloudflare_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    api_base = get_cloudflare_api_base()
    if not api_base:
        raise Exception("Cloudflare API Base 未配置")
    deadline = time.time() + timeout
    # 同一封邮件正文可能延迟可读，允许多次重试解析，避免偶发漏码
    seen_attempts = {}
    next_resend_at = time.time() + 35
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[Debug] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 35
        try:
            messages = cloudflare_get_messages(api_base, dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Cloudflare 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] Cloudflare 本轮邮件数量: {len(messages)}")

        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id:
                continue
            attempt = int(seen_attempts.get(msg_id, 0))
            if attempt >= 5:
                continue
            seen_attempts[msg_id] = attempt + 1
            to_raw = msg.get("to")
            recipients = []
            if isinstance(to_raw, str):
                recipients.append(to_raw.lower())
            elif isinstance(to_raw, list):
                for t in to_raw:
                    if isinstance(t, str):
                        recipients.append(t.lower())
                    elif isinstance(t, dict):
                        recipients.append(t.get("address", "").lower())
            msg_addr = str(msg.get("address", "")).lower()
            # 优先匹配目标邮箱；若结构不一致也允许继续解析，避免接口字段漂移导致漏码
            address_matched = True
            if recipients:
                address_matched = email.lower() in recipients
            elif msg_addr:
                address_matched = msg_addr == email.lower()
            if not address_matched and log_callback:
                log_callback(f"[Debug] 跳过疑似非目标邮件 id={msg_id} address={msg_addr} to={recipients}")
                continue
            parts = []
            # 先直接从列表项取内容，避免 detail 接口差异导致漏码
            for field in ("text", "raw", "content", "intro", "body", "snippet"):
                value = msg.get(field)
                if isinstance(value, str) and value.strip():
                    parts.append(value)
            html_list = msg.get("html") or []
            if isinstance(html_list, str):
                html_list = [html_list]
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            subject = str(msg.get("subject", "") or "")
            combined = "\n".join(parts)
            # 再尝试 detail 接口补全内容
            try:
                detail = cloudflare_get_message_detail(api_base, dev_token, msg_id)
                for field in ("text", "raw", "content", "intro", "body", "snippet"):
                    value = detail.get(field)
                    if isinstance(value, str) and value.strip():
                        combined += "\n" + value
                html_list2 = detail.get("html") or []
                if isinstance(html_list2, str):
                    html_list2 = [html_list2]
                for h in html_list2:
                    combined += "\n" + re.sub(r"<[^>]+>", " ", h)
                if not subject:
                    subject = str(detail.get("subject", "") or "")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] Cloudflare detail接口失败，改用列表内容解析: {exc}")
            if log_callback:
                log_callback(f"[Debug] Cloudflare 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[Debug] Cloudflare 从邮件中提取到验证码: {code}")
                return code
            elif log_callback:
                log_callback(f"[Debug] 邮件已解析但未提取到验证码 id={msg_id} attempt={seen_attempts[msg_id]}")
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"Cloudflare 在 {timeout}s 内未收到验证码邮件")


def generate_random_birthdate():
    import datetime as dt

    today = dt.date.today()
    age = random.randint(20, 40)
    birth_year = today.year - age
    birth_month = random.randint(1, 12)
    birth_day = random.randint(1, 28)
    return f"{birth_year}-{birth_month:02d}-{birth_day:02d}T16:00:00.000Z"


def response_preview(res, limit=200):
    try:
        text = str(res.text or "")
    except Exception:
        text = ""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def is_cloudflare_block_response(res):
    try:
        headers = {str(k).lower(): str(v).lower() for k, v in dict(res.headers).items()}
        text = str(res.text or "").lower()
        server = headers.get("server", "")
        content_type = headers.get("content-type", "")
        return (
            res.status_code in (403, 429, 503)
            and (
                "cloudflare" in server
                or "cloudflare" in text
                or "cf-error" in text
                or "__cf_chl" in text
                or "text/html" in content_type
            )
        )
    except Exception:
        return False


def set_birth_date(session, log_callback=None):
    url = "https://grok.com/rest/auth/set-birth-date"
    new_headers = {
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    payload = {"birthDate": generate_random_birthdate()}
    try:
        res = session.post(url, json=payload, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(
                f"[Debug] set_birth_date status: {res.status_code}, body: {response_preview(res)}"
            )
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "set_birth_date 被 grok.com 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"set_birth_date HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_birth_date] 异常: {e}")
        return False, f"set_birth_date 异常: {e}"


def set_tos_accepted(session, log_callback=None, timeout=30):
    url = "https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion"
    payload = struct.pack("B", (2 << 3) | 0) + struct.pack("B", 1)
    data = b"\x00" + struct.pack(">I", len(payload)) + payload
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "origin": "https://accounts.x.ai",
        "referer": "https://accounts.x.ai/accept-tos",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=timeout)
        if log_callback:
            log_callback(f"[Debug] set_tos_accepted status: {res.status_code}")
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "set_tos_accepted 被 accounts.x.ai 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"set_tos_accepted HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_tos_accepted] 异常: {e}")
        return False, f"set_tos_accepted 异常: {e}"


def accept_tos_for_token(token, cf_clearance="", log_callback=None, max_attempts=4, retry_delay=2.0):
    """尝试接受 TOS；配置代理后所有尝试固定走该代理，不回退直连。"""
    user_agent = get_user_agent()
    last_message = "set_tos_accepted 未执行"
    proxy_url = str(config.get("proxy") or "").strip()
    proxy_dict = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    modes = [("proxy", proxy_dict)] if proxy_dict else [("direct", None)]
    schedule = []
    while len(schedule) < max_attempts:
        for item in modes:
            schedule.append(item)
            if len(schedule) >= max_attempts:
                break
    for attempt, (mode, proxies) in enumerate(schedule, 1):
        try:
            with requests.Session(impersonate="chrome120", proxies=proxies) as session:
                cookie_parts = [f"sso={token}", f"sso-rw={token}"]
                if cf_clearance:
                    cookie_parts.append(f"cf_clearance={cf_clearance}")
                session.headers.update({
                    "user-agent": user_agent,
                    "cookie": "; ".join(cookie_parts),
                })
                if log_callback:
                    log_callback(f"[*] TOS 尝试 {attempt}/{len(schedule)} via {mode}")
                ok, message = set_tos_accepted(session, log_callback=log_callback, timeout=30)
                if ok:
                    if log_callback:
                        log_callback(f"[+] TOS 已接受（{mode}，第 {attempt}/{len(schedule)} 次）")
                    return True, "ok"
                last_message = f"{mode}: {message}"
                if log_callback:
                    log_callback(f"[!] TOS 未成功（{mode}，第 {attempt}/{len(schedule)} 次）: {message}")
        except Exception as e:
            last_message = f"{mode}: accept_tos_for_token 异常: {e}"
            if log_callback:
                log_callback(f"[!] TOS 异常（{mode}，第 {attempt}/{len(schedule)} 次）: {e}")
        if attempt < len(schedule):
            time.sleep(retry_delay)
    return False, last_message


def encode_grpc_nsfw_settings():
    field1_content = bytes([0x10, 0x01])
    field1 = bytes([0x0A, len(field1_content)]) + field1_content
    nsfw_string = b"always_show_nsfw_content"
    field2_inner = bytes([0x0A, len(nsfw_string)]) + nsfw_string
    field2 = bytes([0x12, len(field2_inner)]) + field2_inner
    payload = field1 + field2
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def update_nsfw_settings(session, log_callback=None):
    url = "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls"
    data = encode_grpc_nsfw_settings()
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(
                f"[Debug] update_nsfw status: {res.status_code}, body: {response_preview(res)}"
            )
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "update_nsfw_settings 被 grok.com 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"update_nsfw_settings HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[update_nsfw] 异常: {e}")
        return False, f"update_nsfw_settings 异常: {e}"


def browser_activate_chat_permission(browser_session, sso_token, log_callback=None, cancel_callback=None, timeout=90):
    """强制浏览器通过 tos-gate：写入 SSO，点击同意，必须离开 tos-gate 才算成功。"""
    log = log_callback or (lambda *_: None)
    if browser_session is None or getattr(browser_session, "page", None) is None:
        return False, "browser session 不可用"
    if not sso_token:
        return False, "sso token 为空"

    page = browser_session.page
    token = str(sso_token).strip()
    last_detail = ""

    def current_url():
        try:
            return str(getattr(page, "url", "") or "")
        except Exception:
            return ""

    def page_body(limit=240):
        try:
            return str(
                page.run_js(
                    "return (document.body && document.body.innerText || '').replace(/\\s+/g,' ').trim().slice(0, arguments[0]);",
                    limit,
                )
                or ""
            )
        except Exception:
            return ""

    def inject_sso_cookies():
        cookies = [
            {"name": "sso", "value": token, "domain": ".x.ai", "path": "/", "secure": True, "httpOnly": True},
            {"name": "sso-rw", "value": token, "domain": ".x.ai", "path": "/", "secure": True, "httpOnly": True},
            {"name": "sso", "value": token, "domain": ".grok.com", "path": "/", "secure": True, "httpOnly": True},
            {"name": "sso-rw", "value": token, "domain": ".grok.com", "path": "/", "secure": True, "httpOnly": True},
            {"name": "sso", "value": token, "domain": "accounts.x.ai", "path": "/", "secure": True, "httpOnly": True},
            {"name": "sso-rw", "value": token, "domain": "accounts.x.ai", "path": "/", "secure": True, "httpOnly": True},
            {"name": "sso", "value": token, "domain": "grok.com", "path": "/", "secure": True, "httpOnly": True},
            {"name": "sso-rw", "value": token, "domain": "grok.com", "path": "/", "secure": True, "httpOnly": True},
        ]
        try:
            page.set.cookies(cookies)
        except Exception:
            for c in cookies:
                try:
                    page.set.cookies(c)
                except Exception:
                    pass

    def click_accept_like_buttons():
        return page.run_js(
            r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (!style || style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
  return [
    node.innerText,
    node.textContent,
    node.getAttribute('aria-label'),
    node.getAttribute('title'),
    node.getAttribute('value'),
    node.getAttribute('data-testid'),
    node.getAttribute('name'),
  ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function score(t) {
  const raw = String(t || '');
  const s = raw.toLowerCase().replace(/\s+/g, '');
  let n = 0;
  // policy/document links are NOT accept buttons
  if (s.includes('acceptableusepolicy') || s.includes('privacypolicy') || s.includes('termsofservice') || s.includes('policy') && (s.includes('view') || s.includes('read') || raw.length > 28)) n -= 150;
  if (s.includes('服务条款') || s.includes('使用政策') || s.includes('隐私政策')) n -= 80;
  if (s.includes('acceptandcontinue') || s.includes('acceptcontinue') || s.includes('iaccept') || s.includes('acceptall')) n += 100;
  if ((s === 'accept' || s.startsWith('accept')) && !s.includes('policy')) n += 90;
  if (s.includes('agree') || s.includes('iagree') || s.includes('agreeto')) n += 85;
  if (s.includes('同意') || s.includes('接受') || s.includes('确认并继续') || s.includes('我同意')) n += 90;
  if (s.includes('continue') || s.includes('继续') || s.includes('start') || s.includes('开始') || s.includes('next') || s.includes('下一步')) n += 40;
  if (s.includes('gotit') || s.includes('ok') || s.includes('好的') || s.includes('done') || s.includes('完成')) n += 25;
  if (s.includes('cancel') || s.includes('取消') || s.includes('decline') || s.includes('拒绝') || s.includes('later') || s.includes('skip')) n -= 120;
  // short primary buttons preferred
  if (raw.trim().length > 0 && raw.trim().length <= 18) n += 15;
  return n;
}
// prefer main buttons, then any clickable
const selectors = [
  'button[type="submit"]',
  'button',
  'input[type="submit"]',
  '[role="button"]',
  'div[role="button"]',
  // anchors last: often policy links, not accept actions
  'a[href]',
];
const seen = new Set();
const nodes = [];
for (const sel of selectors) {
  for (const n of Array.from(document.querySelectorAll(sel))) {
    if (seen.has(n)) continue;
    seen.add(n);
    nodes.push(n);
  }
}
const ranked = nodes
  .filter((n) => isVisible(n) && !n.disabled && n.getAttribute('aria-disabled') !== 'true')
  .map((n) => ({ n, t: textOf(n), s: score(textOf(n)) }))
  .filter((x) => x.s > 0)
  .sort((a, b) => b.s - a.s);
const body = (document.body && document.body.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 220);
if (!ranked.length) {
  return {
    state: 'no-button',
    url: location.href,
    title: document.title,
    body,
    buttons: Array.from(document.querySelectorAll('button,[role="button"],input[type="submit"]'))
      .filter(isVisible).map(textOf).filter(Boolean).slice(0, 8),
  };
}
const best = ranked[0];
try { best.n.scrollIntoView({block:'center', inline:'center'}); } catch (e) {}
try { best.n.focus(); } catch (e) {}
try { best.n.click(); } catch (e) {
  try {
    best.n.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
  } catch (e2) {}
}
// also tick any visible checkboxes/terms toggles before/after
for (const box of Array.from(document.querySelectorAll('input[type="checkbox"]'))) {
  if (isVisible(box) && !box.checked && !box.disabled) {
    try { box.click(); } catch (e) {}
  }
}
return {
  state: 'clicked',
  text: best.t.slice(0, 100),
  score: best.s,
  url: location.href,
  body,
};
"""
        )

    def is_gate_url(url):
        low = str(url or "").lower()
        return ("tos-gate" in low) or ("accept-tos" in low) or ("/tos" in low and "grok.com" in low)

    def is_login_wall(url, body=""):
        low = str(url or "").lower()
        b = str(body or "").lower()
        if any(x in low for x in ("sign-in", "sign-up", "login", "accounts.x.ai/sign")):
            return True
        if "sign in" in b or "登录" in b or "log in" in b:
            return True
        return False

    def has_sso_cookie():
        try:
            cookies_now = page.cookies(all_domains=True, all_info=True) or []
        except Exception:
            return False
        for item in cookies_now:
            name = str(item.get("name", "") if isinstance(item, dict) else getattr(item, "name", "")).strip()
            value = str(item.get("value", "") if isinstance(item, dict) else getattr(item, "value", "")).strip()
            if name == "sso" and value:
                return True
        return False

    def chat_ready(url, body=""):
        low = str(url or "").lower()
        b = str(body or "").lower()
        if not low or is_gate_url(low) or is_login_wall(low, b):
            return False
        if "grok.com" not in low:
            return False
        # negative markers of unfinished TOS
        if "terms of service" in b and ("accept" in b or "agree" in b):
            return False
        if "服务条款" in b and ("接受" in b or "同意" in b):
            return False
        return has_sso_cookie()

    def hard_click_pass_gate(stage):
        nonlocal last_detail, page
        clicked = click_accept_like_buttons()
        if isinstance(clicked, dict):
            state = clicked.get("state")
            if state == "clicked":
                log(f"[*] 浏览器 {stage} 点击: {clicked.get('text') or 'button'}")
                last_detail = f"{stage}:clicked:{clicked.get('text')}"
            else:
                buttons = clicked.get("buttons") or []
                body = clicked.get("body") or ""
                last_detail = f"{stage}:no-button body={str(body)[:80]} buttons={buttons[:4]}"
                log(f"[Debug] 浏览器 {stage} 无按钮: {last_detail}")
            return clicked
        last_detail = f"{stage}:click-result={clicked}"
        return clicked

    try:
        # 1) seed cookies on both domains
        try:
            page.get("https://accounts.x.ai/")
            sleep_with_cancel(0.8, cancel_callback)
        except Exception:
            pass
        inject_sso_cookies()
        log("[*] 浏览器已写入 sso cookie，开始强制过 TOS 门禁")

        targets = [
            "https://accounts.x.ai/accept-tos",
            "https://grok.com/tos-gate",
            "https://grok.com/",
        ]
        deadline = time.time() + max(45, int(timeout or 90))
        attempt = 0
        while time.time() < deadline:
            raise_if_cancelled(cancel_callback)
            attempt += 1
            target = targets[(attempt - 1) % len(targets)]
            try:
                browser_session.refresh_page()
                page = browser_session.page
            except Exception:
                pass
            try:
                inject_sso_cookies()
                page.get(target)
            except Exception as e:
                last_detail = f"open {target} failed: {e}"
                log(f"[!] 浏览器打开失败: {last_detail}")
                sleep_with_cancel(1.0, cancel_callback)
                continue

            sleep_with_cancel(1.2, cancel_callback)
            # multi-click rounds on current page
            for round_i in range(1, 8):
                raise_if_cancelled(cancel_callback)
                url = current_url()
                body = page_body()
                if chat_ready(url, body):
                    log(f"[+] 浏览器已离开 TOS 门禁: {url[:140]}")
                    return True, f"browser left gate: {url[:140]}"
                if is_gate_url(url) or ("accept" in body.lower() and "term" in body.lower()) or ("同意" in body) or ("接受" in body):
                    hard_click_pass_gate(f"gate-r{round_i}")
                    sleep_with_cancel(1.4, cancel_callback)
                    url2 = current_url()
                    body2 = page_body()
                    if chat_ready(url2, body2):
                        log(f"[+] 浏览器点击后离开 TOS 门禁: {url2[:140]}")
                        return True, f"browser left gate after click: {url2[:140]}"
                else:
                    # not obviously a gate; still try one accept-like click then re-check
                    hard_click_pass_gate(f"page-r{round_i}")
                    sleep_with_cancel(1.2, cancel_callback)
                    url2 = current_url()
                    body2 = page_body()
                    if chat_ready(url2, body2):
                        log(f"[+] 浏览器页面激活完成: {url2[:140]}")
                        return True, f"browser ready: {url2[:140]}"
                    # if redirected to gate, keep looping
                    if is_gate_url(url2):
                        continue
                    break

            # small pause before next target cycle
            sleep_with_cancel(0.8, cancel_callback)

        # final check
        url = current_url()
        body = page_body()
        if chat_ready(url, body):
            log(f"[+] 浏览器 TOS/chat 激活完成: {url[:140]}")
            return True, f"browser ok: {url[:140]}"
        return False, f"仍未离开 TOS 门禁, last_url={url[:160]}, detail={last_detail[:160]}, body={body[:100]}"
    except Exception as e:
        return False, f"browser_activate_chat_permission 异常: {e}"


def browser_set_birth_date(
    browser_session,
    log_callback=None,
    cancel_callback=None,
    attempts=3,
    retry_delay=2.0,
):
    """Set the birth date through the authenticated grok.com browser session."""
    log = log_callback or (lambda *_: None)
    if browser_session is None or getattr(browser_session, "page", None) is None:
        return False, "browser session 不可用"

    page = browser_session.page
    last_message = "browser birth 未执行"
    for attempt in range(1, max(1, int(attempts)) + 1):
        raise_if_cancelled(cancel_callback)
        try:
            if "grok.com" not in str(getattr(page, "url", "") or "").lower():
                page.get("https://grok.com/")
                sleep_with_cancel(1.0, cancel_callback)
            result = page.run_js(
                r"""
return fetch('/rest/auth/set-birth-date', {
  method: 'POST',
  credentials: 'include',
  headers: {'content-type': 'application/json'},
  body: JSON.stringify({birthDate: arguments[0]}),
}).then(async (response) => ({
  ok: response.ok,
  status: response.status,
  body: (await response.text()).slice(0, 160),
})).catch((error) => ({
  ok: false,
  status: 0,
  error: String(error).slice(0, 160),
}));
""",
                generate_random_birthdate(),
            )
            status = int(result.get("status") or 0) if isinstance(result, dict) else 0
            if isinstance(result, dict) and result.get("ok") and 200 <= status < 300:
                log(f"[+] 浏览器出生日期已设置（第 {attempt} 次，HTTP {status}）")
                return True, "ok"
            last_message = f"browser set_birth_date HTTP {status}"
            log(f"[!] 浏览器出生日期设置失败（第 {attempt} 次）: {last_message}")
        except Exception as exc:
            last_message = f"browser_set_birth_date 异常: {exc}"
            log(f"[!] 浏览器出生日期设置异常（第 {attempt} 次）: {exc}")
        if attempt < max(1, int(attempts)):
            try:
                page.get("https://grok.com/")
                sleep_with_cancel(1.0, cancel_callback)
                browser_session.refresh_page()
                page = browser_session.page
            except Exception:
                pass
            sleep_with_cancel(retry_delay, cancel_callback)
    return False, last_message


def browser_chat_canary(
    browser_session,
    log_callback=None,
    cancel_callback=None,
    timeout=60,
):
    """Send a browser chat canary and require an assistant reply marker."""
    log = log_callback or (lambda *_: None)
    if browser_session is None or getattr(browser_session, "page", None) is None:
        return False, "browser session 不可用"

    page = browser_session.page
    marker = "WEB_CANARY_" + secrets.token_hex(12)
    try:
        page.get("https://grok.com/")
        surface_deadline = time.time() + min(30, max(10, int(timeout or 60) // 2))
        surface_state = {}
        while time.time() < surface_deadline:
            raise_if_cancelled(cancel_callback)
            try:
                surface_state = page.run_js(
                    r"""
function visible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  const style = getComputedStyle(node);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
}
const editor = Array.from(document.querySelectorAll('textarea,[contenteditable="true"]')).find(visible);
const send = document.querySelector('button[data-testid="chat-submit"]');
return {
  ready: Boolean(editor),
  hasEditor: Boolean(editor),
  hasSend: Boolean(send),
  sendDisabled: Boolean(send && send.disabled),
  readyState: document.readyState,
  url: location.href,
};
"""
                )
            except Exception as exc:
                surface_state = {"error": type(exc).__name__, "url": str(getattr(page, "url", ""))}
            if isinstance(surface_state, dict) and surface_state.get("ready"):
                break
            current_url = str((surface_state or {}).get("url") or getattr(page, "url", ""))
            if "tos-gate" in current_url or "/login" in current_url:
                return False, f"网页对话界面被门禁阻断: {current_url[:160]}"
            sleep_with_cancel(0.5, cancel_callback)
        else:
            detail = surface_state if isinstance(surface_state, dict) else {}
            return False, (
                "网页对话界面等待超时: "
                f"url={str(detail.get('url') or getattr(page, 'url', ''))[:140]}, "
                f"readyState={detail.get('readyState')}, editor={detail.get('hasEditor')}, "
                f"send={detail.get('hasSend')}, disabled={detail.get('sendDisabled')}"
            )

        editor_result = page.run_js(
            r"""
const marker = arguments[0];
function visible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  const style = getComputedStyle(node);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
}
const editor = Array.from(document.querySelectorAll('textarea,[contenteditable="true"]')).find(visible);
if (!editor) return {sent: false, reason: 'no-editor', url: location.href};
const value = 'Reply exactly: ' + marker;
editor.focus();
if (editor.tagName === 'TEXTAREA' || editor.tagName === 'INPUT') {
  const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(editor), 'value');
  if (descriptor && descriptor.set) descriptor.set.call(editor, value);
  else editor.value = value;
} else {
  editor.textContent = value;
}
editor.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
editor.dispatchEvent(new Event('change', {bubbles: true}));
return {filled: true, url: location.href};
""",
            marker,
        )
        if not isinstance(editor_result, dict) or not editor_result.get("filled"):
            return False, f"网页对话填写失败: {(editor_result or {}).get('reason', 'unknown')}"
        send_deadline = time.time() + 10
        send_result = {}
        while time.time() < send_deadline:
            raise_if_cancelled(cancel_callback)
            send_result = page.run_js(
                r"""
function visible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  const style = getComputedStyle(node);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
}
const send = document.querySelector('button[data-testid="chat-submit"]');
if (!send || !visible(send) || send.disabled) return {sent: false, reason: 'chat-submit-unavailable'};
send.click();
return {sent: true, via: 'chat-submit', url: location.href};
"""
            )
            if isinstance(send_result, dict) and send_result.get("sent"):
                break
            sleep_with_cancel(0.5, cancel_callback)
        else:
            return False, f"网页对话发送失败: {(send_result or {}).get('reason', 'unknown')}"

        submit_deadline = time.time() + 5
        while time.time() < submit_deadline:
            raise_if_cancelled(cancel_callback)
            submitted = page.run_js(
                r"""
const marker = arguments[0];
const userSelectors = [
  '[data-message-author-role="user"]',
  '[data-role="user"]',
  '[data-testid*="user-message"]',
];
const userMatch = userSelectors.some((selector) =>
  Array.from(document.querySelectorAll(selector)).some((node) => (node.innerText || '').includes(marker))
);
const editor = Array.from(document.querySelectorAll('textarea,[contenteditable="true"]')).find((node) => {
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
});
const editorText = editor && (editor.value || editor.innerText || editor.textContent) || '';
const body = document.body && document.body.innerText || '';
return {
  submitted: userMatch || (!editorText.includes(marker) && body.includes(marker)),
  permissionDenied: /permission-denied|access to the chat endpoint is denied/i.test(body),
  url: location.href,
};
""",
                marker,
            )
            if isinstance(submitted, dict) and submitted.get("permissionDenied"):
                return False, "网页对话首次提交返回 PERMISSION_DENIED/403"
            if isinstance(submitted, dict) and submitted.get("submitted"):
                break
            sleep_with_cancel(0.5, cancel_callback)
        else:
            return False, "网页对话未确认真实提交"

        deadline = time.time() + max(20, int(timeout or 60))
        while time.time() < deadline:
            raise_if_cancelled(cancel_callback)
            result = page.run_js(
                r"""
const marker = arguments[0];
const assistantSelectors = [
  '[data-message-author-role="assistant"]',
  '[data-role="assistant"]',
  '[data-testid*="assistant"]',
];
const assistantMatch = assistantSelectors.some((selector) =>
  Array.from(document.querySelectorAll(selector)).some((node) => (node.innerText || '').includes(marker))
);
const body = document.body && document.body.innerText || '';
return {
  assistantMatch,
  permissionDenied: /permission-denied|access to the chat endpoint is denied/i.test(body),
  url: location.href,
};
""",
                marker,
            )
            if isinstance(result, dict) and result.get("permissionDenied"):
                return False, "网页对话首次提交返回 PERMISSION_DENIED/403"
            if isinstance(result, dict) and result.get("assistantMatch"):
                log("[+] 网页对话 canary 已收到 assistant 精确回复")
                return True, "ok"
            sleep_with_cancel(1.5, cancel_callback)
        return False, "网页对话 canary 等待 assistant 回复超时"
    except Exception as exc:
        return False, f"browser_chat_canary 异常: {exc}"


def enable_nsfw_for_token(
    token,
    cf_clearance="",
    log_callback=None,
    tos_already_accepted=False,
    birth_already_set=False,
):
    proxies = get_proxies()
    user_agent = get_user_agent()
    try:
        with requests.Session(impersonate="chrome120", proxies=proxies) as session:
            cookie_parts = [f"sso={token}", f"sso-rw={token}"]
            if cf_clearance:
                cookie_parts.append(f"cf_clearance={cf_clearance}")
            session.headers.update(
                {
                    "user-agent": user_agent,
                    "cookie": "; ".join(cookie_parts),
                }
            )
            if not tos_already_accepted:
                ok, message = set_tos_accepted(session, log_callback)
                if not ok:
                    return False, message
            if not birth_already_set:
                ok, message = set_birth_date(session, log_callback)
                if not ok:
                    return False, message
            ok, message = update_nsfw_settings(session, log_callback)
            if not ok:
                return False, message
            return True, "成功开启 NSFW"
    except Exception as e:
        return False, f"异常: {str(e)}"


SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"


class BrowserSession:
    """每个并发 worker 独立持有一个浏览器实例，取代原来的全局 browser/page。"""

    # 屏蔽的静态资源（省带宽）；刻意不含 Turnstile 依赖的脚本/域名
    BLOCKED_URLS = [
        "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.svg", "*.ico", "*.bmp",
        "*.woff", "*.woff2", "*.ttf", "*.otf", "*.eot",
        "*.mp4", "*.webm", "*.m4v", "*.mp3", "*.ogg", "*.wav",
    ]

    # 内联 turnstilePatch 扩展的核心补丁：在每个页面/子 frame 的最早期伪造
    # MouseEvent 的 screenX/screenY（随机屏幕基准 + clientX/Y），堵住 Cloudflare
    # 检测自动化鼠标事件的通道。用 add_init_js 全局注入，无需外部 turnstilePatch 目录。
    STEALTH_INIT_JS = r"""
(function () {
  try {
    function _rnd(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
    var _sx = _rnd(800, 1200);
    var _sy = _rnd(400, 600);
    Object.defineProperty(MouseEvent.prototype, 'screenX', {
      configurable: true,
      get: function () { return _sx + this.clientX; }
    });
    Object.defineProperty(MouseEvent.prototype, 'screenY', {
      configurable: true,
      get: function () { return _sy + this.clientY; }
    });
  } catch (e) {}
})();
"""

    def __init__(self, log_callback=None):
        self.browser = None
        self.page = None
        self.log_callback = log_callback

    def _log(self, message):
        if self.log_callback:
            self.log_callback(message)

    def _apply_bandwidth_rules(self):
        # 页面级拦截字体/媒体等静态资源省带宽（可选，默认关；可能影响验证渲染）
        if self.page is None or not config.get("block_media_fonts", False):
            return
        try:
            self.page.set.blocked_urls(self.BLOCKED_URLS)
        except Exception:
            pass

    def _apply_stealth_patch(self):
        # 内联 turnstilePatch 补丁（可选，默认关；会改主文档指纹，可能弄巧成拙）
        if self.page is None or not config.get("stealth_patch", False):
            return
        try:
            self.page.add_init_js(self.STEALTH_INIT_JS)
        except Exception:
            pass

    def start(self):
        last_exc = None
        for attempt in range(1, 5):
            try:
                self.browser = Chromium(create_browser_options())
                try:
                    self._pid = self.browser.process_id
                except Exception:
                    self._pid = None
                try:
                    self._port = str(self.browser.address).rsplit(":", 1)[-1].strip()
                except Exception:
                    self._port = None
                tabs = self.browser.get_tabs()
                self.page = tabs[-1] if tabs else self.browser.new_tab()
                # 仅当开启了页面级钩子时才预热（确保 CDP 就绪）；
                # 默认全关时不预热，start 行为与最初脚本完全一致
                if config.get("block_media_fonts", False) or config.get(
                    "stealth_patch", False
                ):
                    try:
                        self.page.get("about:blank")
                    except Exception:
                        pass
                self._apply_bandwidth_rules()
                self._apply_stealth_patch()
                if config.get("hide_window", False):
                    for _ in range(5):
                        if _hide_chrome_session_windows(self._pid, self._port):
                            break
                        time.sleep(0.2)
                if attempt > 1:
                    self._log(f"[Debug] 浏览器第 {attempt} 次启动成功")
                return self.browser, self.page
            except Exception as exc:
                last_exc = exc
                self._log(f"[Debug] 浏览器启动失败(第{attempt}/4次): {exc}")
                try:
                    if self.browser is not None:
                        self.browser.quit(del_data=True)
                except Exception:
                    pass
                self.browser = None
                self.page = None
                time.sleep(min(1.5 * attempt, 4))
        raise Exception(f"浏览器启动失败，已重试4次: {last_exc}")

    def stop(self):
        pid = getattr(self, "_pid", None)
        port = getattr(self, "_port", None)
        if self.browser is not None:
            try:
                self.browser.quit(del_data=True)
            except Exception:
                pass
        # quit 关不干净时兜底：按调试端口定位真正的 chrome 主进程并强杀整棵树
        _kill_chrome_session(pid, port)
        self.browser = None
        self.page = None
        self._pid = None
        self._port = None

    def restart(self):
        self.stop()
        gc.collect()
        return self.start()

    def refresh_page(self):
        if self.browser is None:
            self.restart()
            return self.page
        try:
            tabs = self.browser.get_tabs()
            self.page = tabs[-1] if tabs else self.browser.new_tab()
            self._apply_bandwidth_rules()
        except Exception:
            self.restart()
        return self.page


def click_email_signup_button(session, timeout=10, log_callback=None, cancel_callback=None):
    page = session.page
    deadline = time.time() + timeout
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if log_callback:
            log_callback("[Debug] 尝试查找“使用邮箱注册”按钮...")

        clicked = page.run_js(r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function nodeText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('href'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function scoreEntry(node) {
    const compact = nodeText(node).replace(/\s+/g, '');
    const lower = compact.toLowerCase();
    if (compact.includes('使用邮箱注册')) return 100;
    if (lower.includes('signupwithemail')) return 95;
    if (lower.includes('continuewithemail')) return 90;
    if (lower.includes('email') && (lower.includes('sign') || lower.includes('continue') || lower.includes('use') || lower.includes('with'))) return 80;
    if (lower === 'email' || lower.includes('邮箱')) return 70;
    return 0;
}
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map((node) => ({ node, score: scoreEntry(node), text: nodeText(node) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score);
const target = candidates[0]?.node || null;
if (!target) {
    return false;
}
target.click();
return candidates[0].text || true;
        """)

        if clicked:
            if log_callback:
                detail = f": {clicked}" if isinstance(clicked, str) else ""
                log_callback(f"[Debug] 已点击「使用邮箱注册」按钮{detail}")
            sleep_with_cancel(2, cancel_callback)
            return True

        if log_callback:
            current_url = page.url if page else "none"
            log_callback(f"[Debug] 当前URL: {current_url}")

        sleep_with_cancel(1, cancel_callback)

    if log_callback:
        page_html = page.html[:500] if page else "no page"
        log_callback(f"[Debug] 页面内容片段: {page_html}")

    raise Exception("未找到「使用邮箱注册」按钮")


def open_signup_page(session, log_callback=None, cancel_callback=None):
    raise_if_cancelled(cancel_callback)
    if session.browser is None:
        session.start()
        if log_callback:
            log_callback("[Debug] 浏览器已启动")
    try:
        page = session.browser.get_tab(0)
        page.get(SIGNUP_URL)
    except Exception as e:
        if log_callback:
            log_callback(f"[Debug] 打开URL异常: {e}")
        try:
            page = session.browser.new_tab(SIGNUP_URL)
        except Exception as e2:
            if log_callback:
                log_callback(f"[Debug] 创建新标签页异常: {e2}")
            session.restart()
            page = session.browser.new_tab(SIGNUP_URL)
    session.page = page
    session._apply_bandwidth_rules()
    page.wait.doc_loaded()
    sleep_with_cancel(2, cancel_callback)
    if log_callback:
        log_callback(f"[Debug] 当前URL: {page.url}")
    click_email_signup_button(
        session, log_callback=log_callback, cancel_callback=cancel_callback
    )


def has_profile_form(session, log_callback=None):
    session.refresh_page()
    page = session.page
    try:
        return bool(
            page.run_js(
                """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
            """
            )
        )
    except Exception:
        return False


def fill_email_and_submit(session, timeout=45, log_callback=None, cancel_callback=None):
    page = session.page
    raise_if_cancelled(cancel_callback)
    email, dev_token = get_email_and_token()
    if not email or not dev_token:
        raise Exception("获取邮箱失败")
    if log_callback:
        log_callback(f"[Debug] 已创建邮箱: {email}")
    deadline = time.time() + timeout
    last_diag_time = 0
    last_reclick_time = 0
    last_snapshot = None
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        filled = page.run_js(
            r"""
const email = arguments[0];
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('placeholder'),
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('id'),
        node.getAttribute('autocomplete'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function describeInput(node) {
    return [
        `type=${node.getAttribute('type') || ''}`,
        `name=${node.getAttribute('name') || ''}`,
        `id=${node.getAttribute('id') || ''}`,
        `placeholder=${node.getAttribute('placeholder') || ''}`,
        `aria=${node.getAttribute('aria-label') || ''}`,
        `testid=${node.getAttribute('data-testid') || ''}`,
    ].join(' ').replace(/\s+/g, ' ').trim().slice(0, 160);
}
function describeAction(node) {
    return textOf(node).slice(0, 120);
}
function emailCandidates() {
    const direct = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'));
    const all = Array.from(document.querySelectorAll('input, textarea'));
    for (const node of all) {
        const type = (node.getAttribute('type') || '').toLowerCase();
        if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'search'].includes(type)) continue;
        const meta = textOf(node).toLowerCase();
        if (meta.includes('email') || meta.includes('e-mail') || meta.includes('mail') || meta.includes('邮箱') || meta.includes('电子邮件')) {
            direct.push(node);
        }
    }
    return Array.from(new Set(direct));
}
const visibleInputs = Array.from(document.querySelectorAll('input, textarea'))
    .filter((node) => isVisible(node) && !node.disabled && !node.readOnly)
    .map(describeInput)
    .slice(0, 8);
const visibleActions = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map(describeAction)
    .filter(Boolean)
    .slice(0, 10);
const input = emailCandidates().find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input) {
    return {
        state: 'not-ready',
        url: location.href,
        title: document.title,
        inputs: visibleInputs,
        buttons: visibleActions,
    };
}
input.focus(); input.click();
const valueProto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
const valueSetter = Object.getOwnPropertyDescriptor(valueProto, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) tracker.setValue('');
if (valueSetter) valueSetter.call(input, email); else input.value = email;
input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new InputEvent('input', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new Event('change', { bubbles: true }));
const inputType = (input.getAttribute('type') || '').toLowerCase();
const isValid = inputType !== 'email' || input.checkValidity();
if ((input.value || '').trim() !== email || !isValid) {
    return {
        state: 'fill-failed',
        value: input.value || '',
        valid: isValid,
        input: describeInput(input),
        url: location.href,
    };
}
input.blur();
return {
    state: 'filled',
    input: describeInput(input),
    url: location.href,
};
            """,
            email,
        )
        state = filled.get("state") if isinstance(filled, dict) else filled
        if isinstance(filled, dict):
            last_snapshot = filled
        if state == "not-ready":
            now = time.time()
            if now - last_reclick_time >= 3:
                reclicked = page.run_js(r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function nodeText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('href'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function scoreEntry(node) {
    const compact = nodeText(node).replace(/\s+/g, '');
    const lower = compact.toLowerCase();
    if (compact.includes('使用邮箱注册')) return 100;
    if (lower.includes('signupwithemail')) return 95;
    if (lower.includes('continuewithemail')) return 90;
    if (lower.includes('email') && (lower.includes('sign') || lower.includes('continue') || lower.includes('use') || lower.includes('with'))) return 80;
    if (lower === 'email' || lower.includes('邮箱')) return 70;
    return 0;
}
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map((node) => ({ node, score: scoreEntry(node), text: nodeText(node) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score);
if (!candidates.length) return false;
candidates[0].node.click();
return candidates[0].text || true;
                """)
                last_reclick_time = now
                if reclicked and log_callback:
                    detail = f": {reclicked}" if isinstance(reclicked, str) else ""
                    log_callback(f"[Debug] 邮箱输入框未出现，已再次触发邮箱注册入口{detail}")
            if log_callback and now - last_diag_time >= 5:
                last_diag_time = now
                inputs = " | ".join((filled or {}).get("inputs", [])[:6]) if isinstance(filled, dict) else ""
                buttons = " | ".join((filled or {}).get("buttons", [])[:8]) if isinstance(filled, dict) else ""
                url = (filled or {}).get("url", page.url if page else "") if isinstance(filled, dict) else (page.url if page else "")
                log_callback(f"[Debug] 等待邮箱输入框: url={url}; inputs={inputs or 'none'}; buttons={buttons or 'none'}")
            sleep_with_cancel(0.5, cancel_callback)
            continue
        if state != "filled":
            if log_callback:
                log_callback(f"[Debug] 邮箱输入框已出现，但写入失败: {filled}")
            sleep_with_cancel(0.5, cancel_callback)
            continue
        sleep_with_cancel(0.8, cancel_callback)
        clicked = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('placeholder'),
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('id'),
        node.getAttribute('autocomplete'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function emailCandidates() {
    const direct = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'));
    const all = Array.from(document.querySelectorAll('input, textarea'));
    for (const node of all) {
        const type = (node.getAttribute('type') || '').toLowerCase();
        if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'search'].includes(type)) continue;
        const meta = textOf(node).toLowerCase();
        if (meta.includes('email') || meta.includes('e-mail') || meta.includes('mail') || meta.includes('邮箱') || meta.includes('电子邮件')) {
            direct.push(node);
        }
    }
    return Array.from(new Set(direct));
}
const input = emailCandidates().find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input || !(input.value || '').trim()) return false;
const inputType = (input.getAttribute('type') || '').toLowerCase();
if (inputType === 'email' && !input.checkValidity()) return false;
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true');
const submitButton = buttons.find((node) => {
    const text = textOf(node).replace(/\s+/g, '');
    const lower = text.toLowerCase();
    return (
        text === '注册' ||
        text.includes('注册') ||
        text.includes('继续') ||
        text.includes('下一步') ||
        text.includes('确认') ||
        lower.includes('signup') ||
        lower.includes('sign up') ||
        lower.includes('continue') ||
        lower.includes('next') ||
        lower.includes('createaccount') ||
        lower.includes('submit')
    );
});
if (submitButton) {
    submitButton.click();
    return textOf(submitButton) || true;
}
const form = input.closest('form');
if (form) {
    if (form.requestSubmit) form.requestSubmit();
    else form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    return 'form-submit';
}
input.focus();
input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
return 'enter';
            """
        )
        if clicked:
            if log_callback:
                detail = f" ({clicked})" if isinstance(clicked, str) else ""
                log_callback(f"[Debug] 已填写邮箱并提交: {email}{detail}")
            return email, dev_token
        sleep_with_cancel(0.5, cancel_callback)
    if last_snapshot:
        inputs = " | ".join(last_snapshot.get("inputs", [])[:6])
        buttons = " | ".join(last_snapshot.get("buttons", [])[:8])
        url = last_snapshot.get("url", page.url if page else "")
        raise Exception(
            f"未找到邮箱输入框或注册按钮，最后页面: url={url}; inputs={inputs or 'none'}; buttons={buttons or 'none'}"
        )
    raise Exception("未找到邮箱输入框或注册按钮")


def fill_code_and_submit(session, email, dev_token, timeout=180, log_callback=None, cancel_callback=None):
    page = session.page

    def _resend_code():
        page.run_js(
            r"""
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = nodes.find((node) => {
  const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
  return t.includes('重新发送') || t.includes('resend') || t.includes('再次发送');
});
if (target && !target.disabled) { target.click(); return true; }
return false;
            """
        )

    code = get_oai_code(
        dev_token,
        email,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
        resend_callback=_resend_code,
    )
    if not code:
        raise Exception("获取验证码失败")
    clean_code = str(code).replace("-", "").strip()
    deadline = time.time() + timeout

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        filled = page.run_js(
            """
const code = String(arguments[0] || '').trim();
if (!code) return 'empty-code';

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setInputValue(input, value) {
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const aggregate = Array.from(document.querySelectorAll(
  'input[data-input-otp=\"true\"], input[name=\"code\"], input[autocomplete=\"one-time-code\"], input[inputmode=\"numeric\"], input[inputmode=\"text\"]'
)).find((node) => isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 6) > 1);

if (aggregate) {
    aggregate.focus();
    aggregate.click();
    setInputValue(aggregate, code);
    return String(aggregate.value || '').replace(/\\s+/g, '') ? 'filled-aggregate' : 'aggregate-failed';
}

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) return false;
    const maxLength = Number(node.maxLength || 0);
    const ac = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || ac === 'one-time-code';
});

if (otpBoxes.length >= code.length) {
    for (let i = 0; i < code.length; i += 1) {
        const ch = code[i] || '';
        const box = otpBoxes[i];
        box.focus();
        box.click();
        setInputValue(box, ch);
        box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: ch }));
        box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: ch }));
    }
    const merged = otpBoxes.slice(0, code.length).map((x) => String(x.value || '').trim()).join('');
    return merged.length ? 'filled-boxes' : 'boxes-failed';
}

return 'not-ready';
            """,
            clean_code,
        )

        if filled == "not-ready":
            sleep_with_cancel(0.5, cancel_callback)
            continue
        if "failed" in str(filled):
            if log_callback:
                log_callback(f"[Debug] 验证码填写失败: {filled}")
            sleep_with_cancel(0.5, cancel_callback)
            continue

        clicked = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const buttons = Array.from(document.querySelectorAll('button[type=\"submit\"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});

const btn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return (
        t.includes('确认邮箱') ||
        t.includes('继续') ||
        t.includes('下一步') ||
        t.includes('confirm') ||
        t.includes('continue') ||
        t.includes('next')
    );
});

if (!btn) return 'no-button';
btn.focus();
btn.click();
return 'clicked';
            """
        )

        if clicked == "clicked" or clicked == "no-button":
            if log_callback:
                log_callback(f"[Debug] 已填写验证码并提交: {code}")
            sleep_with_cancel(1.5, cancel_callback)
            return code

        sleep_with_cancel(0.5, cancel_callback)

    raise Exception("验证码已获取，但自动填写/提交失败")


def getTurnstileToken(session, log_callback=None, cancel_callback=None):
    page = session.page
    if page is None:
        raise Exception("页面未就绪，无法执行 Turnstile")

    try:
        page.run_js(
            "try { if (window.turnstile && typeof turnstile.reset === 'function') turnstile.reset(); } catch(e) {}"
        )
    except Exception:
        pass

    for _ in range(0, 20):
        raise_if_cancelled(cancel_callback)
        try:
            token = page.run_js(
                """
try {
  const byInput = String((document.querySelector('input[name="cf-turnstile-response"]') || {}).value || '').trim();
  if (byInput) return byInput;
  if (window.turnstile && typeof turnstile.getResponse === 'function') {
    return String(turnstile.getResponse() || '').trim();
  }
  return '';
} catch(e) { return ''; }
                """
            )
            token = str(token or "").strip()
            if len(token) >= 80:
                if log_callback:
                    log_callback(f"[Debug] Turnstile 已通过，token长度={len(token)}")
                return token

            challenge_input = page.ele("@name=cf-turnstile-response")
            if challenge_input:
                wrapper = challenge_input.parent()
                iframe = None
                try:
                    iframe = wrapper.shadow_root.ele("tag:iframe")
                except Exception:
                    iframe = None
                if iframe:
                    try:
                        iframe.run_js(
                            """
window.dtp = 1;
function getRandomInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
let sx = getRandomInt(800, 1200);
let sy = getRandomInt(400, 700);
Object.defineProperty(MouseEvent.prototype, 'screenX', { value: sx });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: sy });
                            """
                        )
                    except Exception:
                        pass
                    try:
                        body_sr = iframe.ele("tag:body").shadow_root
                        btn = body_sr.ele("tag:input")
                        if btn:
                            btn.click()
                    except Exception:
                        pass
            else:
                # 兜底：尝试触发页面上可见的 Turnstile 容器
                page.run_js(
                    """
const nodes = Array.from(document.querySelectorAll('div,span,iframe')).filter((n) => {
  const txt = (n.className || '') + ' ' + (n.id || '') + ' ' + (n.getAttribute?.('src') || '');
  return String(txt).toLowerCase().includes('turnstile');
});
if (nodes.length && typeof nodes[0].click === 'function') nodes[0].click();
                    """
                )
        except Exception:
            pass
        sleep_with_cancel(1, cancel_callback)

    raise Exception("Turnstile 获取 token 失败")


def build_profile():
    given_name_pool = [
        "Neo", "Ethan", "Liam", "Noah", "Lucas", "Mason", "Ryan", "Leo",
        "Owen", "Aiden", "Elio", "Aron", "Ivan", "Nolan", "Evan", "Kai",
        "Caleb", "Adam", "Ezra", "Miles", "Logan", "Carter", "Hunter", "Jason",
        "Brian", "Dylan", "Alex", "Colin", "Blake", "Gavin", "Henry", "Julian",
        "Kevin", "Louis", "Marcus", "Nathan", "Oscar", "Peter", "Quinn", "Robin",
        "Simon", "Tristan", "Victor", "Wesley", "Xavier", "Yuri", "Zane", "Felix",
        "Aaron", "Damian",
    ]
    family_name_pool = [
        "Lin", "Wang", "Zhao", "Liu", "Chen", "Zhang", "Xu", "Sun",
        "Guo", "He", "Yang", "Wu", "Zhou", "Tang", "Qin", "Shi",
        "Fang", "Peng", "Cao", "Deng", "Fan", "Fu", "Gao", "Han",
        "Hu", "Jiang", "Kong", "Lu", "Ma", "Nie", "Pan", "Qiao",
        "Ren", "Shao", "Tian", "Xie", "Yan", "Yao", "Yu", "Zeng",
        "Bai", "Duan", "Hou", "Jin", "Kang", "Luo", "Mao", "Song",
        "Wei", "Xiong",
    ]
    given_name = random.choice(given_name_pool)
    family_name = random.choice(family_name_pool)
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given_name, family_name, password


def fill_profile_and_submit(session, timeout=120, log_callback=None, cancel_callback=None):
    page = session.page
    given_name, family_name, password = build_profile()
    deadline = time.time() + timeout
    form_filled_once = False
    wait_cf_since = None
    last_cf_retry_at = 0.0

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if not form_filled_once:
            filled = page.run_js(
                """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) return false;
    input.focus();
    input.click();
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.blur();
    return String(input.value || '').trim() === String(value || '').trim();
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"], input[aria-label*="名"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"], input[aria-label*="姓"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]');

if (!givenInput || !familyInput || !passwordInput) return 'not-ready';

const ok1 = setInputValue(givenInput, givenName);
const ok2 = setInputValue(familyInput, familyName);
const ok3 = setInputValue(passwordInput, password);

if (!ok1 || !ok2 || !ok3) return 'fill-failed';

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});

// 必须等待 Cloudflare 校验通过后再提交
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

if (submitBtn) {
    return 'ready-to-submit';
}
return 'filled-no-submit';
            """,
                given_name,
                family_name,
                password,
            )

            if isinstance(filled, str) and filled.startswith("wait-cloudflare"):
                form_filled_once = True
                if log_callback:
                    token_len = filled.split(":", 1)[1] if ":" in filled else "0"
                    log_callback(f"[Debug] 资料已填写，等待 Cloudflare 人机验证通过... 当前token长度={token_len}")
                if token_len == "0":
                    pause_seconds = random.uniform(1, 3)
                    if log_callback:
                        log_callback(f"[Debug] Cloudflare token 为空，暂停 {pause_seconds:.1f}s 后继续检测")
                    sleep_with_cancel(pause_seconds, cancel_callback)
                now = time.time()
                if wait_cf_since is None:
                    wait_cf_since = now
                # 卡住后自动二次复用 Turnstile 组件
                if now - wait_cf_since >= 12 and now - last_cf_retry_at >= 8:
                    if log_callback:
                        log_callback("[Debug] Cloudflare 验证卡住，开始二次复用 Turnstile...")
                    try:
                        token = getTurnstileToken(session, log_callback=log_callback, cancel_callback=cancel_callback)
                        if token:
                            synced = page.run_js(
                                """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                """,
                                token,
                            )
                            if log_callback:
                                log_callback(f"[Debug] Turnstile 二次复用完成，回填长度={synced}")
                    except Exception as cf_exc:
                        if log_callback:
                            log_callback(f"[Debug] Turnstile 二次复用失败: {cf_exc}")
                    last_cf_retry_at = now
                sleep_with_cancel(0.8, cancel_callback)
                continue

            if filled in ("ready-to-submit", "filled-no-submit"):
                form_filled_once = True
            elif filled == "fill-failed" and log_callback:
                log_callback("[Debug] 资料输入失败，重试中...")
                sleep_with_cancel(0.5, cancel_callback)
                continue
            elif filled == "not-ready":
                sleep_with_cancel(0.5, cancel_callback)
                continue

        submit_state = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

function buttonText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('value'),
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = buttonText(node).replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});
if (!submitBtn) {
    const visibleTexts = buttons.map(buttonText).filter(Boolean).slice(0, 8).join(' | ');
    return 'no-submit-button:' + visibleTexts;
}
submitBtn.focus();
submitBtn.click();
return 'submitted';
            """
        )

        if isinstance(submit_state, str) and submit_state.startswith("wait-cloudflare"):
            if log_callback:
                token_len = submit_state.split(":", 1)[1] if ":" in submit_state else "0"
                log_callback(f"[Debug] 等待 Cloudflare 人机验证通过后再提交... 当前token长度={token_len}")
            now = time.time()
            if wait_cf_since is None:
                wait_cf_since = now
            if now - wait_cf_since >= 12 and now - last_cf_retry_at >= 8:
                if log_callback:
                    log_callback("[Debug] 提交前仍卡住，自动再次复用 Turnstile...")
                try:
                    token = getTurnstileToken(session, log_callback=log_callback, cancel_callback=cancel_callback)
                    if token:
                        synced = page.run_js(
                            """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                            """,
                            token,
                        )
                        if log_callback:
                            log_callback(f"[Debug] Turnstile 二次复用完成，回填长度={synced}")
                except Exception as cf_exc:
                    if log_callback:
                        log_callback(f"[Debug] Turnstile 二次复用失败: {cf_exc}")
                last_cf_retry_at = now
            sleep_with_cancel(0.8, cancel_callback)
            continue

        if submit_state == "submitted":
            if log_callback:
                log_callback(f"[Debug] 已填写注册资料并提交: {given_name} {family_name}")
            return {"given_name": given_name, "family_name": family_name, "password": password}
        wait_cf_since = None
        if isinstance(submit_state, str) and submit_state.startswith("no-submit-button") and log_callback:
            visible_buttons = submit_state.split(":", 1)[1] if ":" in submit_state else ""
            suffix = f" 可见按钮: {visible_buttons}" if visible_buttons else ""
            log_callback(f"[Debug] 未找到提交按钮，继续等待页面稳定...{suffix}")

        sleep_with_cancel(0.5, cancel_callback)

    raise Exception("最终注册页资料填写失败")


def wait_for_sso_cookie(session, timeout=120, log_callback=None, cancel_callback=None):
    page = session.page
    deadline = time.time() + timeout
    last_seen_names = set()
    last_submit_retry = 0.0
    last_cf_retry_at = 0.0
    final_no_submit_state = ""
    final_no_submit_since = None
    final_no_submit_timeout = 25

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            session.refresh_page()
            page = session.page
            if page is None:
                sleep_with_cancel(1, cancel_callback)
                continue

            # 仍停留在“完成注册”页时，若 Cloudflare 已通过，周期性重试点击提交
            now = time.time()
            if now - last_submit_retry >= 2.5:
                retried = page.run_js(
                    r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const titleHit = !!Array.from(document.querySelectorAll('h1,h2,div,span')).find((el) => {
    const t = (el.textContent || '').replace(/\s+/g, '');
    const lower = t.toLowerCase();
    return t.includes('完成注册') || lower.includes('completeyoursignup') || lower.includes('completesignup');
});
if (!titleHit) return 'not-final-page';

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solved = token.length >= 80;
    if (!solved) return 'final-page-wait-cf:' + token.length;
}

function buttonText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('value'),
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = buttonText(node).replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});
if (!submitBtn) {
    const visibleTexts = buttons.map(buttonText).filter(Boolean).slice(0, 8).join(' | ');
    return 'final-page-no-submit:' + visibleTexts;
}
submitBtn.focus();
submitBtn.click();
return 'final-page-clicked-submit';
                    """
                )
                last_submit_retry = now
                if log_callback and (retried == "final-page-clicked-submit" or (isinstance(retried, str) and retried.startswith("final-page-no-submit"))):
                    log_callback(f"[Debug] 最终页状态: {retried}")
                if isinstance(retried, str) and retried.startswith("final-page-no-submit"):
                    if retried != final_no_submit_state:
                        final_no_submit_state = retried
                        final_no_submit_since = now
                    elif final_no_submit_since and now - final_no_submit_since >= final_no_submit_timeout:
                        raise AccountRetryNeeded(
                            f"最终注册页状态 {final_no_submit_timeout}s 未变化且未找到提交按钮，重试当前账号: {retried}"
                        )
                else:
                    final_no_submit_state = ""
                    final_no_submit_since = None
                if log_callback and isinstance(retried, str) and retried.startswith("final-page-wait-cf"):
                    token_len = retried.split(":", 1)[1] if ":" in retried else "0"
                    log_callback(f"[Debug] 最终页状态: final-page-wait-cf, token长度={token_len}")
                    if now - last_cf_retry_at >= 10:
                        if log_callback:
                            log_callback("[Debug] 最终页 Cloudflare 卡住，自动二次复用 Turnstile...")
                        try:
                            token = getTurnstileToken(session, log_callback=log_callback, cancel_callback=cancel_callback)
                            if token:
                                synced = page.run_js(
                                    """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                    """,
                                    token,
                                )
                                if log_callback:
                                    log_callback(f"[Debug] 最终页 Turnstile 二次复用完成，回填长度={synced}")
                        except Exception as cf_exc:
                            if log_callback:
                                log_callback(f"[Debug] 最终页 Turnstile 二次复用失败: {cf_exc}")
                        last_cf_retry_at = now

            cookies = page.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()

                if name:
                    last_seen_names.add(name)

                if name == "sso" and value:
                    if log_callback:
                        log_callback("[Debug] 已获取到 sso cookie")
                    return value
        except PageDisconnectedError:
            session.refresh_page()
            page = session.page
        except AccountRetryNeeded:
            raise
        except Exception:
            pass

        sleep_with_cancel(1, cancel_callback)

    raise Exception(
        f"等待超时：未获取到 sso cookie。已看到 cookies: {sorted(last_seen_names)}"
    )


def cli_log(message):
    text = str(message)
    # [Debug] 细节日志一律不输出，CLI 只保留必要信息
    if "[Debug]" in text:
        return
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {text}", flush=True)


class SharedState:
    """并发 worker 间共享的状态：名额发放、计数、文件与日志写入，全部加锁。"""

    def __init__(self, count, accounts_output_file, target_successes=0):
        self.count = count
        self.target_successes = max(0, int(target_successes or 0))
        self.accounts_output_file = accounts_output_file
        configured_mail_file = str(config.get("mail_credentials_file") or "").strip()
        self.mail_cred_file = configured_mail_file or os.path.join(
            os.path.dirname(__file__), "mail_credentials.txt"
        )
        configured_success_file = str(config.get("success_records_file") or "").strip()
        self.success_records_file = configured_success_file
        self._issued = 0  # 已发放的名额数（总共发放 count 个）
        self.success_count = 0
        self.fail_count = 0
        self.stop_requested = False
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()

    def claim_slot(self):
        """领取一个注册名额，返回 (是否成功, 序号)。"""
        with self._lock:
            if (
                self.stop_requested
                or self._issued >= self.count
                or (self.target_successes and self.success_count >= self.target_successes)
            ):
                return False, 0
            self._issued += 1
            return True, self._issued

    def record_success(self):
        with self._lock:
            self.success_count += 1
            return self.success_count

    def record_fail(self):
        with self._lock:
            self.fail_count += 1
            return self.fail_count

    def should_stop(self):
        return self.stop_requested

    def stop(self):
        self.stop_requested = True

    def target_reached(self):
        with self._lock:
            return bool(
                self.target_successes and self.success_count >= self.target_successes
            )

    def log(self, worker_id, message):
        with self._io_lock:
            cli_log(f"[W{worker_id}] {message}")

    def save_account(self, line):
        with self._io_lock:
            try:
                fd = os.open(self.accounts_output_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                os.chmod(self.accounts_output_file, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception as exc:
                cli_log(f"[Debug] 保存账号文件失败: {exc}")

    def save_mail_credential(self, email, dev_token):
        with self._io_lock:
            try:
                fd = os.open(self.mail_cred_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                os.chmod(self.mail_cred_file, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(f"{email}\t{dev_token}\n")
            except Exception:
                pass

    def save_success_record(self, record):
        if not self.success_records_file:
            return
        with self._io_lock:
            fd = os.open(
                self.success_records_file,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            os.chmod(self.success_records_file, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def register_one(session, shared, worker_id, slot_no):
    """完成一个账号的完整注册流程（原串行循环体的一次迭代）。"""

    def log(msg):
        shared.log(worker_id, msg)

    cancel = shared.should_stop
    email = ""
    dev_token = ""
    code = ""
    mail_ok = False
    max_mail_retry = 3
    for mail_try in range(1, max_mail_retry + 1):
        open_signup_page(session, log_callback=log, cancel_callback=cancel)
        email, dev_token = fill_email_and_submit(
            session, log_callback=log, cancel_callback=cancel
        )
        log(f"[*] 邮箱: {email}")
        shared.save_mail_credential(email, dev_token)
        try:
            code = fill_code_and_submit(
                session, email, dev_token, log_callback=log, cancel_callback=cancel
            )
            mail_ok = True
            break
        except Exception as mail_exc:
            msg = str(mail_exc)
            if ("未收到验证码" in msg or "验证码" in msg) and mail_try < max_mail_retry:
                log(f"[!] 本邮箱未取到验证码，自动更换新邮箱重试: {msg}")
                session.restart()
                sleep_with_cancel(1, cancel)
                continue
            raise

    if not mail_ok:
        raise Exception("验证码阶段失败，已达到最大重试次数")
    profile = fill_profile_and_submit(session, log_callback=log, cancel_callback=cancel)
    sso = wait_for_sso_cookie(session, log_callback=log, cancel_callback=cancel)
    # TOS、生日和网页对话均为硬门禁；OAuth 后的客户端 preprobe 是最终可用性门禁。
    api_ok, api_msg = accept_tos_for_token(sso, log_callback=log, max_attempts=4, retry_delay=2.0)
    browser_ok, browser_msg = browser_activate_chat_permission(
        session, sso, log_callback=log, cancel_callback=cancel, timeout=90
    )
    if not browser_ok:
        # 再给一次机会：重试浏览器门禁
        log(f"[!] 浏览器首次未过门禁，重试一次: {browser_msg}")
        browser_ok, browser_msg = browser_activate_chat_permission(
            session, sso, log_callback=log, cancel_callback=cancel, timeout=60
        )
    if not browser_ok:
        raise RuntimeError(
            f"TOS 门禁未通过，账号不可用: browser={browser_msg}; api={api_msg}"
        )
    birth_ok, birth_msg = browser_set_birth_date(
        session,
        log_callback=log,
        cancel_callback=cancel,
        attempts=3,
        retry_delay=2.0,
    )
    if not birth_ok:
        raise RuntimeError(f"出生日期设置未通过，账号不可用: {birth_msg}")
    chat_ok, chat_msg = browser_chat_canary(
        session,
        log_callback=log,
        cancel_callback=cancel,
        timeout=60,
    )
    if not chat_ok:
        raise RuntimeError(f"网页对话验证未通过，账号不可用: {chat_msg}")
    if api_ok and browser_ok:
        log(f"[+] API TOS + 浏览器已离开 tos-gate: {browser_msg}")
    elif browser_ok:
        log(f"[+] 浏览器已离开 tos-gate（API TOS 未确认: {api_msg}）")
    if config.get("enable_nsfw", True):
        # NSFW 为增强项；TOS 和 birth 已强制成功，这里失败不阻断注册。
        nsfw_ok, nsfw_msg = enable_nsfw_for_token(
            sso,
            log_callback=log,
            tos_already_accepted=browser_ok,
            birth_already_set=birth_ok,
        )
        if not nsfw_ok and log:
            log(f"[!] NSFW/birth 未完全成功（TOS 已通过，继续）: {nsfw_msg}")
    password = profile.get("password", "")
    shared.save_account(f"{email}----{password}----{sso}\n")
    export_result = export_cpa_after_register(
        email, password, session=session, sso=sso, log_callback=log
    )
    if not export_result.get("ok"):
        raise RuntimeError(f"OAuth/Sub2API auth 导出失败: {export_result.get('error') or export_result}")
    if config.get("cpa_push_enabled", False) and not export_result.get("pushed"):
        raise RuntimeError(f"Sub2API auth 推送失败: {export_result.get('push_error') or export_result}")
    response = export_result.get("push_response") or {}
    if config.get("cpa_require_probe_passed", False):
        if response.get("probe") != "passed":
            raise RuntimeError(f"bridge 未确认 probe=passed: {response or export_result}")
    if config.get("cpa_require_created", False):
        if response.get("action") != "created":
            raise RuntimeError(
                f"bridge action 不是 created，不能计为新增账号: {response or export_result}"
            )
    return {
        "email": email,
        "account_id": response.get("account_id"),
        "action": response.get("action"),
        "probe": response.get("probe"),
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def register_worker(worker_id, shared):
    """单个并发 worker：自建独立浏览器，循环领取名额并注册。"""

    def log(msg):
        shared.log(worker_id, msg)

    session = BrowserSession(log_callback=log)
    try:
        session.start()
        log("[Debug] 浏览器已启动")
    except Exception as exc:
        log(f"[!] 浏览器启动失败，worker 退出: {exc}")
        return

    max_slot_retry = 3
    try:
        while not shared.should_stop():
            ok, slot_no = shared.claim_slot()
            if not ok:
                break
            log(f"--- 开始第 {slot_no}/{shared.count} 个账号 ---")
            retry_count_for_slot = 0
            while True:
                try:
                    success = register_one(session, shared, worker_id, slot_no)
                    shared.save_success_record(success)
                    total = shared.record_success()
                    log(f"[+] 注册成功: {success['email']}（累计成功 {total}）")
                    break
                except RegistrationCancelled:
                    raise
                except AccountRetryNeeded as exc:
                    retry_count_for_slot += 1
                    if retry_count_for_slot <= max_slot_retry:
                        log(
                            f"[!] 当前账号流程卡住，重试第 {retry_count_for_slot}/{max_slot_retry} 次: {exc}"
                        )
                        session.restart()
                        sleep_with_cancel(1, shared.should_stop)
                        continue
                    total = shared.record_fail()
                    log(f"[-] 当前账号已达到最大重试次数，跳过: {exc}（累计失败 {total}）")
                    break
                except Exception as exc:
                    import traceback
                    log(f"[!] 完整错误追踪: {traceback.format_exc()}")
                    total = shared.record_fail()
                    log(f"[-] 注册失败: {exc}（累计失败 {total}）")
                    break
            if shared.should_stop():
                break
            session.restart()
    except RegistrationCancelled:
        log("[!] 注册被停止")
    except Exception as exc:
        log(f"[!] worker 异常退出: {exc}")
    finally:
        session.stop()
        cleanup_mint_browsers()
        log("[Debug] worker 结束")


def run_registration_concurrent(count, concurrency, target_successes=0):
    output_dir = str(config.get("accounts_output_dir") or "").strip()
    if not output_dir:
        output_dir = os.path.dirname(__file__)
    output_dir = os.path.abspath(os.path.expanduser(output_dir))
    os.makedirs(output_dir, mode=0o700, exist_ok=True)
    os.chmod(output_dir, 0o700)
    accounts_output_file = os.path.join(
        output_dir,
        f"accounts_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
    )
    max_concurrency = max(1, int(config.get("max_concurrency", 1) or 1))
    concurrency = max(1, min(concurrency, count, max_concurrency))
    shared = SharedState(count, accounts_output_file, target_successes=target_successes)
    target_text = f"，成功目标 {shared.target_successes}" if shared.target_successes else ""
    cli_log(f"[*] 开始并发注册，最多尝试 {count} 个{target_text}，并发 {concurrency} 个浏览器")
    cli_log(f"[*] 成功账号将实时保存到: {accounts_output_file}")
    threads = []
    for worker_id in range(concurrency):
        t = threading.Thread(
            target=register_worker, args=(worker_id, shared), daemon=True
        )
        t.start()
        threads.append(t)
        time.sleep(1)  # 错开各 worker 启动，降低同一出口 IP 的瞬时并发压力
    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=0.5)
    except KeyboardInterrupt:
        shared.stop()
        cli_log("[!] 收到 Ctrl+C，正在停止所有 worker 并清理...")
        for t in threads:
            t.join()
    cleanup_stray_chrome()
    cli_log(f"[*] 任务结束。成功 {shared.success_count} | 失败 {shared.fail_count}")
    return {
        "attempted": shared._issued,
        "successes": shared.success_count,
        "failures": shared.fail_count,
        "target_successes": shared.target_successes,
        "target_reached": shared.target_reached() if shared.target_successes else True,
        "accounts_output_file": accounts_output_file,
    }


def main():
    global CONFIG_FILE
    parser = argparse.ArgumentParser(description="Register Grok accounts through the browser client")
    parser.add_argument("--config")
    parser.add_argument("--count", type=int)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--target-successes", type=int)
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args()
    if args.config:
        CONFIG_FILE = os.path.abspath(os.path.expanduser(args.config))
    load_config()
    count = int(args.count if args.count is not None else config.get("register_count", 1) or 1)
    target_successes = int(
        args.target_successes
        if args.target_successes is not None
        else config.get("target_successes", 0) or 0
    )
    if count < 1:
        raise SystemExit("--count/register_count must be at least 1")
    if target_successes < 0 or target_successes > count:
        raise SystemExit("target_successes must be between 0 and count")
    cli_log("[*] 已加载配置")
    cli_log(
        f"[*] 当前邮箱服务商: {config.get('email_provider', 'duckmail')} | "
        f"最多尝试数: {count} | 成功目标: {target_successes or '未设置'}"
    )
    if config.get("cpa_export_enabled", True):
        push = "开" if config.get("cpa_push_enabled", False) else "关"
        cli_log(
            f"[*] Sub2API auth 导出: 开启 | 目录: {config.get('cpa_auth_dir', './cpa_auths')} "
            f"| 远端推送: {push}"
            + (f" -> {config.get('cpa_remote_base')}" if config.get("cpa_push_enabled") else "")
        )
    else:
        cli_log("[*] Sub2API auth 导出: 关闭（仅写 accounts_*.txt）")
    if args.concurrency is not None:
        concurrency = args.concurrency
    elif args.non_interactive:
        concurrency = int(config.get("max_concurrency", 1) or 1)
    else:
        try:
            raw = input("请输入并发数量（同时开几个浏览器，直接回车=1）: ").strip()
        except (KeyboardInterrupt, EOFError):
            cli_log("[!] 已取消")
            return 130
        try:
            concurrency = int(raw) if raw else 1
        except ValueError:
            cli_log("[!] 并发数量无效，使用 1")
            concurrency = 1
    if concurrency < 1:
        concurrency = 1
    summary = run_registration_concurrent(
        count,
        concurrency,
        target_successes=target_successes,
    )
    print("GROK_CLIENT_SUMMARY=" + json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary["target_reached"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
