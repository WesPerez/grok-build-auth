"""注册成功钩子：铸造 Grok Build 设备码 OIDC → 客户端 preprobe → 写出 Sub2API
xai-<email>.json → 推送到远端 hardened bridge。

- 本地写盘目录：config['cpa_auth_dir']（默认 ./cpa_auths）
- 远端推送：POST config['cpa_remote_base'] + /v0/management/auth-files?name=...
  认证 X-Management-Key: config['cpa_remote_secret']

免费 Grok 4.5 用 base_url=cli-chat-proxy；Sub2API 请求 grok 时自带
x-grok-client-version 头，免费号不会 426。

客户端 preprobe（默认开启）：
在 write_cpa_xai_auth / push 之前直连 Grok CLI /v1/responses。
只有 decision=pass 才进入正式 cpa_auths 并推送；PERMISSION_DENIED /
网络抖动 / 429 等写入 cpa_pending 或 cpa_cooldown，不进正式目录、不 push。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

_REG_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _REG_DIR.parents[1]
_DEFAULT_OUT = _REG_DIR / "cpa_auths"


def _resolve_out_dir(cfg: dict) -> Path:
    raw = str(cfg.get("cpa_auth_dir") or _DEFAULT_OUT).strip()
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (_REG_DIR / p).resolve()
    return p


def _record_failure(out_dir: Path, email: str, reason: str) -> None:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / "cpa_auth_failed.txt", "a", encoding="utf-8") as f:
            f.write(f"{email}----{reason}----{int(time.time())}\n")
    except Exception:
        pass


def _preprobe_enabled(cfg: dict) -> bool:
    # Default ON: avoid writing/pushing tokens that only pass /models.
    required = bool(cfg.get("cpa_preprobe_required", False))
    enabled = bool(cfg.get("cpa_preprobe_enabled", True))
    if required and not enabled:
        raise ValueError("cpa_preprobe_required=true forbids disabling cpa_preprobe")
    return enabled


def _run_preprobe(
    payload: dict,
    *,
    proxy: str,
    cfg: dict,
    log: Callable[[str], None],
) -> tuple[dict, dict]:
    """Return (probe_result, possibly_refreshed_payload)."""
    from cpa.preprobe import probe_auth, try_refresh_access_token

    timeout = float(cfg.get("cpa_preprobe_timeout_sec", 45) or 45)
    attempts = max(1, int(cfg.get("cpa_preprobe_attempts", 3) or 3))
    retry_delay = float(cfg.get("cpa_preprobe_retry_delay_sec", 4) or 4)
    permission_retry_delay = float(
        cfg.get("cpa_preprobe_permission_retry_delay_sec", 0) or 0
    )
    soft_retry_codes = {
        "PROBE_NETWORK_ERROR",
        "INVALID_RESPONSE",
        "INCOMPLETE_RESPONSE",
        "UPSTREAM_ERROR",
    }
    if permission_retry_delay > 0:
        soft_retry_codes.add("PERMISSION_DENIED")
    probe: dict = {}
    for attempt in range(1, attempts + 1):
        probe = probe_auth(payload, proxy=proxy or "", timeout=timeout)
        decision = probe.get("decision")
        code = str(probe.get("code") or "")
        status = probe.get("status")
        log(
            f"[cpa] preprobe attempt={attempt}/{attempts} "
            f"decision={decision} code={code} status={status}"
        )
        if decision == "pass":
            break
        if decision == "reject":
            break
        if decision in ("cooldown", "refresh"):
            break
        if code not in soft_retry_codes:
            break
        if attempt < attempts:
            time.sleep(
                permission_retry_delay
                if code == "PERMISSION_DENIED"
                else retry_delay
            )

    decision = probe.get("decision")
    code = probe.get("code")
    status = probe.get("status")

    if decision == "refresh" and bool(cfg.get("cpa_preprobe_refresh_on_invalid", True)):
        refreshed = try_refresh_access_token(payload, proxy=proxy or "", timeout=30)
        if refreshed.get("ok") and isinstance(refreshed.get("auth"), dict):
            payload = refreshed["auth"]
            probe = probe_auth(payload, proxy=proxy or "", timeout=timeout)
            decision = probe.get("decision")
            code = probe.get("code")
            status = probe.get("status")
            log(f"[cpa] preprobe after refresh decision={decision} code={code} status={status}")
        else:
            log(
                f"[cpa] preprobe refresh failed code={refreshed.get('code')} "
                f"status={refreshed.get('status')}"
            )
            # Keep original refresh decision for routing.
            probe = {
                "decision": "refresh",
                "code": refreshed.get("code") or "TOKEN_INVALID",
                "status": refreshed.get("status") or 0,
            }
    return probe, payload


# ── 主入口 ──

def export_cpa_for_account(
    email: str,
    password: str,
    *,
    page: Any | None = None,
    config: dict | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> dict:
    """在注册浏览器里铸造 OIDC → preprobe → 写本地 xai-<email>.json → 推送远端。

    复用注册成功后仍开着、且已登录 grok 的浏览器铸造，不另开浏览器。
    返回 {ok, email, path, pushed, push_status?, error?, preprobe?}。
    """
    cfg = config or {}
    log = log_callback or (lambda m: print(m, flush=True))

    if not cfg.get("cpa_export_enabled", True):
        log("[cpa] 已关闭导出，跳过")
        return {"ok": False, "skipped": True, "reason": "disabled"}
    email = (email or "").strip()
    if not email or not password:
        return {"ok": False, "error": "缺少 email/password", "email": email}
    if page is None:
        log("[!] 无可复用的注册浏览器，跳过铸造")
        return {"ok": False, "error": "no register browser page", "email": email}

    from oidc_mint import mint_with_browser, resolve_proxy, set_runtime_proxy
    import cpa

    out_dir = _resolve_out_dir(cfg)

    # 代理优先级：mint_proxy > proxy > 环境（preprobe 也走同一条，不用 cpa_push_proxy）
    proxy = (cfg.get("mint_proxy") or cfg.get("proxy") or "").strip()
    if not proxy:
        proxy = (
            os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
            or os.environ.get("http_proxy") or ""
        ).strip()
    resolved = resolve_proxy(proxy or None)
    set_runtime_proxy(resolved or None)

    timeout = float(cfg.get("mint_timeout_sec", 300) or 300)
    base_url = cfg.get("cpa_base_url") or cpa.CLI_BASE_URL

    # 复用注册浏览器铸造：它已登录 grok，开设备码页直接确认，不另开浏览器
    try:
        tokens = mint_with_browser(
            email=email,
            password=password,
            page=page,
            proxy=resolved or None,
            browser_timeout_sec=timeout,
            force_standalone=False,
            cookies=None,
            poll_log=lambda m: log(f"[Debug] {m}"),
        )
    except Exception as exc:  # noqa: BLE001
        log(f"[!] 设备码铸造失败，切换 SSO 协议 OAuth: {exc}")
        try:
            if str(_PROJECT_DIR) not in sys.path:
                sys.path.insert(0, str(_PROJECT_DIR))
            from xconsole_client.xai_oauth import complete_build_oauth

            raw_cookies = page.cookies(all_domains=True, all_info=True) or []
            session_cookies = {
                str(item.get("name")): str(item.get("value"))
                for item in raw_cookies
                if isinstance(item, dict) and item.get("name") and item.get("value") is not None
            }
            if not session_cookies.get("sso"):
                raise RuntimeError("registered browser has no SSO cookie")
            oauth_result = complete_build_oauth(
                email=email,
                password=password,
                protocol=True,
                playwright_fallback=False,
                proxy=resolved or "",
                session_cookies=session_cookies,
                timeout=timeout,
            )
            tokens = dict(oauth_result.token)
            log("[Debug] SSO 协议 OAuth 成功")
        except Exception as fallback_exc:  # noqa: BLE001
            reason = f"device={exc}; protocol={fallback_exc}"
            _record_failure(out_dir, email, reason)
            if cfg.get("mint_required", False):
                raise RuntimeError("OAuth mint failed in both device and protocol modes") from fallback_exc
            return {"ok": False, "error": reason, "email": email}

    try:
        payload = cpa.build_cpa_xai_auth(
            email=email,
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            id_token=tokens.get("id_token"),
            expires_in=tokens.get("expires_in"),
            base_url=base_url,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"[!] 组装 Sub2API auth payload 失败: {exc}")
        _record_failure(out_dir, email, f"build: {exc}")
        if cfg.get("mint_required", False):
            raise
        return {"ok": False, "error": str(exc), "email": email}

    # ── 客户端 preprobe：正式落盘 / push 前先证明 chat 可用 ──
    preprobe_meta: dict[str, Any] | None = None
    if _preprobe_enabled(cfg):
        probe, payload = _run_preprobe(
            payload,
            proxy=str(resolved or ""),
            cfg=cfg,
            log=log,
        )
        preprobe_meta = {
            "decision": probe.get("decision"),
            "code": probe.get("code"),
            "status": probe.get("status"),
        }
        decision = str(probe.get("decision") or "")
        if decision != "pass":
            reason = f"preprobe:{probe.get('code')}:{probe.get('status')}"
            _record_failure(out_dir, email, reason)

            if decision == "reject":
                return {
                    "ok": False,
                    "email": email,
                    "error": str(probe.get("code") or "MALFORMED_AUTH"),
                    "preprobe": preprobe_meta,
                }

            # retry / cooldown / refresh → 旁路目录，绝不进正式 auth 或 push
            side_name = "cpa_cooldown" if decision == "cooldown" else "cpa_pending"
            pending_dir = out_dir.parent / side_name
            try:
                target = "cooldown" if decision == "cooldown" else "pending"
                pending_path = cpa.transition(out_dir.parent, payload, target)
            except Exception as exc:  # noqa: BLE001
                log(f"[!] 写 {side_name} 失败: {exc}")
                if cfg.get("mint_required", False) or cfg.get("cpa_preprobe_required", True):
                    raise
                return {
                    "ok": False,
                    "email": email,
                    "error": f"{reason}; write_{side_name}:{exc}",
                    "preprobe": preprobe_meta,
                }
            log(f"[cpa] preprobe 未通过，已旁路到 {side_name}: {pending_path.name} ({reason})")
            return {
                "ok": False,
                "email": email,
                "path": str(pending_path),
                "error": str(probe.get("code") or reason),
                "preprobe": preprobe_meta,
                "side_dir": side_name,
            }

    # 只有 pass（或关闭 preprobe）才写正式目录
    try:
        path = cpa.transition(out_dir.parent, payload, "verified", verified_dir=out_dir)
        filename = Path(path).name
    except Exception as exc:  # noqa: BLE001
        log(f"[!] 写本地文件失败: {exc}")
        _record_failure(out_dir, email, f"write: {exc}")
        if cfg.get("mint_required", False):
            raise
        return {"ok": False, "error": str(exc), "email": email, "preprobe": preprobe_meta}

    log(f"[Debug] 已写本地: {path}")
    result: dict[str, Any] = {
        "ok": True,
        "email": email,
        "path": str(path),
        "pushed": False,
        "preprobe": preprobe_meta or {"decision": "skipped"},
    }

    # 推送远端 Sub2API bridge（bridge 仍是最终信任边界）
    if cfg.get("cpa_push_enabled", False):
        remote_base = str(cfg.get("cpa_remote_base") or "").strip()
        secret = str(cfg.get("cpa_remote_secret") or "").strip()
        if not remote_base or not secret:
            log("[!] 推送已开启但未配置 cpa_remote_base/cpa_remote_secret，跳过推送")
        else:
            push_proxy = str(cfg.get("cpa_push_proxy") or "").strip() or None
            verify_tls = bool(cfg.get("cpa_remote_verify_tls", True))
            try:
                ok, status, text = cpa.push_auth_file(
                    remote_base=remote_base,
                    secret=secret,
                    filename=filename,
                    payload=payload,
                    proxy=push_proxy,
                    verify_tls=verify_tls,
                    timeout=float(cfg.get("cpa_push_timeout_sec", 240) or 240),
                )
                result["push_status"] = status
                if ok:
                    try:
                        response = json.loads(text)
                    except Exception:
                        response = {"raw": text[:300]}
                    result["push_response"] = response
                    result["pushed"] = True
                    log(f"[Debug] 已推送远端: {remote_base} (HTTP {status})")
                    if cfg.get("cpa_require_probe_passed", False) and response.get("probe") != "passed":
                        result["ok"] = False
                        result["pushed"] = False
                        result["error"] = "bridge response missing probe=passed"
                else:
                    # Safe truncated body for diagnostics; never log tokens from payload.
                    result["push_error"] = text[:300]
                    log(f"[!] [cpa] 推送远端失败 HTTP {status}: {text[:200]}")
                    if cfg.get("cpa_push_required", False):
                        result["ok"] = False
                        result["error"] = f"push HTTP {status}: {text[:200]}"
            except Exception as exc:  # noqa: BLE001
                log(f"[!] [cpa] 推送远端异常: {exc}")
                result["push_error"] = str(exc)
                if cfg.get("cpa_push_required", False):
                    result["ok"] = False
                    result["error"] = f"push: {exc}"

    return result
