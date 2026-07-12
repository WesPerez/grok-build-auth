---
name: grok-sub2api-ops
description: 在 GROKAUTH 项目中通过服务器协议或 Windows/Edge 客户端注册 Grok 账号，铸造或恢复 Grok Build OAuth，推送 auth 到 hardened bridge，审计 429/refresh revoked/422 隔离账号，验证 Sub2API 指定账号和 Responses 可用性，并检查注册专用代理池。用户提到服务器注册、Windows grok-register、Edge CDP 9222、OIDC mint、CPA auth push、Sub2API 增加或恢复 Grok 账号、账号轮询、429 冷却、invalid_grant、bridge 422、代理节点故障或要求从头跑完整链路时使用。
---

# Grok Sub2API Operations

以“Sub2API 中新增或恢复一个真实可用的 Grok OAuth 账号”为完成标准。Web 注册、SSO、本地 auth、HTTP 2xx 或数据库 active 标记都不能单独证明完成。

## 定位与读取

优先使用用户给出的项目路径；否则寻找同时包含以下文件的目录：

```text
OPERATIONS.zh-CN.md
scripts/register_and_import.py
clients/windows/grok_register_ttk.py
scripts/windows_client_preflight.py
```

先读取 `OPERATIONS.zh-CN.md` 对应路径。遇到 Windows UI、CDP、422、429 或 revoked 时再读取 [runbook.md](references/runbook.md)。只使用仓库入口，不运行历史 patch/debug/one-shot 脚本。

## 选择模式

- `server-full`：服务器协议注册。运行 `scripts/register_and_import.py`，单账号、单 worker。
- `client-full`：外部 Windows/受控 Edge 浏览器注册。预检后运行 `clients/windows/grok_register_ttk.py`，首次并发固定为 1。
- `export-only`：账号已在用户 Edge 登录。运行本技能 `scripts/export_logged_in.py`。
- `push-only`：已有完整 `xai-*.json`。运行本技能 `scripts/push_auth.py` 幂等重推。
- `audit-recover`：逐账号指定测试，区分可用、429 冷却、refresh revoked 和隔离，再决定恢复。

## 生产写入门禁

执行前确认目标项目、Sub2API 部署、Grok 分组、账号数量和允许的生产写入。没有明确授权时只运行预检、静态检查和只读状态核对。删除账号、重置密码、改分组、改调度状态或清理邮箱必须获得精确目标授权并先建立恢复点。

## 服务器路径

```bash
python3 scripts/register_and_import.py \
  --count 1 \
  --workers 1 \
  --registration-backend protocol-yescaptcha \
  --failure-policy abort \
  --confirm-production-write
```

成功必须同时满足：manifest 为 `imported-preprobed`；preprobe 通过；账号精确状态正确；`postimport_account_probes.passed=true`；分组 postprobe HTTP 200、completed 且输出匹配。服务器路径不要求 bridge `action=created`。

默认代理池只用于注册、OAuth 和导入前 preprobe，`GROK_BIND_SUB2API_PROXY_AFTER_IMPORT=false`。健康检查排除坏节点；中途断线不盲目重放，保留现场并让下一批使用持久游标的下一个节点。

## 客户端路径

```bash
python scripts/preflight.py \
  --project-dir <grok-auth> \
  --config <client-config> \
  --skip-cdp

cd <grok-auth>/clients/windows
python grok_register_ttk.py
```

输入并发 `1`。配置必须开启 `mint_required`、`cpa_push_required` 和 `cpa_require_probe_passed`。客户端新增账号要求 auth 文件存在、bridge 返回 `probe=passed` 和 `action=created`；再用 Grok 分组 Key 验证 `/v1/responses`。

export-only 和 push-only 分别使用：

```bash
python scripts/export_logged_in.py --project-dir <grok-auth> --config <config> --email <email>
python scripts/push_auth.py --project-dir <grok-auth> --config <config> --auth <xai-auth.json>
```

密码和 API Key只从环境变量、隐藏提示或 `0600` 私有配置读取，不放命令行。

## 账号判读与恢复

- `429 included free usage / rolling 24-hour window`：额度冷却，不是 token 过期；按 reset 或 24 小时临时跳过，不能永久删除。
- `invalid_grant` 且 `Refresh token has been revoked`：旧 refresh token 永久不可刷新。若有密码/SSO，重新登录铸造 OAuth并更新原账号；没有凭据时只能走邮箱密码重置。等待不会恢复。
- bridge `422`：账号已隔离。先看指定账号错误；资格传播或暂时上游故障可等待后重推同一 auth，revoked/过期则必须先铸造新 auth，429 则等待冷却。不能无分类地重复注册。
- push 超时：先对账 bridge 日志、账号 ID 和 token hash，再幂等重推；不能假定服务端未写入。
- `action=updated`：只证明更新已有账号，不能声称账号数量增加。

## 安全与清理

- 不回显 access/refresh/id token、SSO、密码、邮箱 JWT、管理密钥或代理凭据。
- 不关闭用户 Edge、代理、IDE、Codex 或归属不明进程。
- 只清理由当前批 manifest、PID、调试端口或精确路径证明归属的临时配置、虚拟环境和任务浏览器资料。
- 不按 `xai*`、扩展名、关键词或时间批量删除邮箱、auth、日志和目录。

## 输出

报告模式、脱敏邮箱、账号 ID、auth 路径、代理 ref（仅注册阶段）、bridge action/probe 或服务器 manifest、指定账号 probe、分组 Responses、429/revoked 残余状态、清理项、commit 和 push 状态。
