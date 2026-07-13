---
name: grok-sub2api-ops
description: 在 grok-build-auth 项目中通过服务器协议或外部 Windows/Edge 客户端注册 Grok 账号，铸造或恢复 OAuth，验证并通过 hardened bridge 推送 Sub2API auth，对账 Grok 分组、402/429 额度、refresh revoked、permission-denied、未绑定分组、auth 文件与代理池。用户提到服务器协议注册、Windows Grok 注册、Edge CDP、OIDC mint、Sub2API auth push、兼容 cpa_* 配置、bridge 422/error_code、批量账号可用性、remint、导入、清理或完整 Grok 链路时使用。
---

# Grok Sub2API Operations

以“Sub2API 中新增、恢复或确认一个真实可用的 Grok OAuth 账号”为完成标准。Web 注册、SSO、本地 auth、HTTP 2xx、数据库 active 或 schedulable 标记都不能单独证明完成。402/429 额度耗尽仍算可用账号，应保留并等待额度窗口。

## 定位与读取

服务器项目默认位于 `/root/grok-build-auth`；用户给出其他路径时先核验仓库身份。寻找以下入口：

```text
OPERATIONS.zh-CN.md
scripts/register_and_import.py
clients/windows/grok_register_ttk.py
scripts/windows_client_preflight.py
```

先读取仓库 `OPERATIONS.zh-CN.md`。Windows、CDP、422、429 或 revoked 读取 [runbook.md](references/runbook.md)；批量对账、导入、分组和清理读取 [audit-import-pipeline.zh-CN.md](references/audit-import-pipeline.zh-CN.md)。只使用正式入口，不运行历史 patch、debug 或 one-shot 脚本。

## 选择模式

- `server-full`：服务器协议注册，运行 `scripts/register_and_import.py`。这是独立可选方式，不是废弃流程。
- `client-full`：外部 Windows/受控 Edge 注册，运行 `clients/windows/grok_register_ttk.py` 并经 bridge 推送。
- `export-only`：账号已在用户 Edge 登录，运行本技能 `scripts/export_logged_in.py`。
- `push-only`：已有完整 `xai-*.json`，运行本技能 `scripts/push_auth.py` 幂等重推。
- `audit-recover`：逐账号指定测试，区分正常、额度耗尽、revoked、权限错误和瞬时故障，再决定恢复或清理。

服务器不运行 Windows 客户端注册程序。服务器协议和外部 Windows 客户端是并列路径；只有旧的“先 quarantine 写库、再探针筛选”流程废弃。

## 配置发现与写入门禁

先从 systemd unit 的 `EnvironmentFiles`、`/root/grok-build-auth/private/*.env` 和 Sub2API 元数据发现实际 bridge/Sub2API 地址、监听端口、Grok 分组 ID、数据库位置和 credential file。不得假定固定端口、固定组 ID、容器名或密钥路径。

执行生产写入前确认目标部署、Grok 分组、账号范围和授权。删除账号、清理 auth/邮箱、改分组、改调度或重置密码前必须：

1. 建立可验证的数据库恢复点。
2. 输出候选账号 ID、脱敏身份、当前分组和最后探针证据。
3. 逐 ID 确认授权和处置理由。
4. 执行后逐 ID 复核，不使用关键词、扩展名或目录通配删除。

## 服务器协议路径

在 `/root/grok-build-auth` 先执行单账号 canary：

```bash
python3 scripts/register_and_import.py \
  --count 1 \
  --workers 1 \
  --registration-backend protocol-yescaptcha \
  --failure-policy abort \
  --confirm-production-write
```

成功必须同时满足 manifest、导入前 `/responses` preprobe、精确账号状态、指定账号 postimport test 和 Grok 分组 `/v1/responses`。服务器路径不经过 Windows 客户端或 bridge push。

## 外部 Windows 客户端路径

在外部客户端机器运行预检和注册；`--project-dir` 指向该机器的仓库副本，不照抄服务器路径：

```bash
python scripts/preflight.py --project-dir <windows-project-dir> --config <client-config> --skip-cdp
cd <windows-project-dir>/clients/windows
python grok_register_ttk.py
```

面向业务统一称为 Sub2API auth。`cpa_*` 配置键、`cpa_auths/` 目录和 `cpa_export` 模块是现有兼容接口；auth JSON 保持 CLIProxyAPI-compatible schema，不为改名破坏客户端配置。

客户端必须在正式 auth 写盘和 push 前调用 Grok CLI `/v1/responses`，校验 completed 且只从 assistant output 提取随机 nonce。required 门禁不得被 enabled=false 绕过。只有 pass 才进入正式目录并 push；网络、5xx、permission propagation 进入 pending；402/429 进入 cooldown；明确 revoked 才进入 remint 或删除候选。

bridge 是最终信任边界：写库前 preprobe；写库后指定账号 test。postimport 失败时必须核对 `imported`、`action`、账号 ID 和数据库状态，不能把所有 422 都描述成零写入。

## 账号判读

- 指定账号 test completed：保留并调度。
- 402/429 明确额度耗尽：保留，按 reset/cooldown 暂停或等待；仍算可用。
- `invalid_grant` / refresh revoked：有密码、SSO 或邮箱恢复能力时 remint 并更新原账号；否则列为逐 ID 清理候选。
- permission/TOS：等待资格传播后复测；持续失败且有重复证据时列为逐 ID 清理候选。
- SSL、代理、超时、普通 5xx：只算不确定，不能删除。
- 未绑定分组：先判定是否是旧 quarantine 残留、导入中断或人工配置；不能仅凭无组直接删除。

## 安全与输出

不回显 token、SSO、密码、邮箱 JWT、管理密钥或代理凭据；不关闭用户 Edge、代理、IDE、Codex 或归属不明进程。报告模式、脱敏账号、精确账号 ID、探针分类、额度状态、分组绑定、bridge action/imported、恢复点、逐 ID 清理结果以及 commit/push 状态。
