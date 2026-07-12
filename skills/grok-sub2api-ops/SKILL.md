---
name: grok-sub2api-ops
description: 在 GROKAUTH 项目中预检 Windows Grok 客户端，运行单账号浏览器注册，复用已登录 Microsoft Edge 铸造 Grok Build OAuth，推送已有 xAI auth JSON 到 hardened bridge，并验证 Sub2API 账号创建和 Responses 可用性。用户提到 Windows grok-register、Edge CDP 9222、OIDC mint、CPA auth push、bridge probe、Sub2API 增加 Grok 账号、注册成功但未入组、422 隔离恢复或要求从头跑完整链路时使用。
---

# Grok Sub2API Operations

以“Sub2API 中新增或更新一个真实可用的 Grok OAuth 账号”为完成标准。Web 注册、SSO、本地 auth 文件或 management HTTP 2xx 都不是单独的完成证据。

## 定位项目

优先使用用户给出的 GROKAUTH 路径；否则检查当前目录、父目录和常见路径。必须存在：

```text
OPERATIONS.zh-CN.md
clients/windows/grok_register_ttk.py
scripts/windows_client_preflight.py
```

按需读取项目 [完整操作手册](../../OPERATIONS.zh-CN.md) 的“外部客户端路径”和“Sub2API 客户端调用”。遇到 Windows UI、CDP 或错误分支时读取 [runbook.md](references/runbook.md)。

## 选择模式

- `full`：需要新注册账号。先预检，再运行 `clients/windows/grok_register_ttk.py`，并发固定从 1 开始。
- `export-only`：目标账号已在用户 Edge 登录。使用本技能的 `scripts/export_logged_in.py`。
- `push-only`：已有完整 `xai-*.json`。使用 `scripts/push_auth.py` 幂等重推。

不要用实验性的 `export_one.py`、`one_shot_pipeline.py`、patch/debug/test 脚本；它们可能硬编码凭据或产生假成功。

## 执行顺序

1. 确认用户授权的目标账号、客户端目录和 bridge/Sub2API 范围。
2. 检查 Python 3.12/3.13、配置、代理、bridge 和所需 Edge CDP：

```bash
python scripts/preflight.py --project-dir <grok-auth> --config <client-config>
```

3. `full` 模式只运行 1 个账号、并发 1。主程序必须开启 `mint_required`、`cpa_push_required` 和 `cpa_require_probe_passed`。
4. `export-only` 从环境或隐藏提示读取密码，不把密码放命令行：

```bash
python scripts/export_logged_in.py \
  --project-dir <grok-auth> \
  --config <client-config> \
  --email <account-email> \
  --require-created
```

5. `push-only` 使用现有 auth：

```bash
python scripts/push_auth.py \
  --project-dir <grok-auth> \
  --config <client-config> \
  --auth <xai-auth.json> \
  --require-created
```

6. 能取得 Grok 分组 API Key 时，通过环境变量传入并追加：

```text
--responses-base https://<sub2api-domain>
--responses-key-env GROK_GROUP_API_KEY
```

7. 只有以下证据全部成立才报告完成：auth 文件存在；bridge 返回 `probe=passed`；新账号任务返回 `action=created`；可访问时 Responses probe 为 completed 且输出匹配。

## 安全边界

- 不回显 access/refresh/id token、SSO、密码、邮箱 JWT、管理密钥或代理凭据。
- 不把密码和 API Key放入命令行；使用环境变量、受限配置或隐藏提示。
- 不关闭或终止用户 Edge、代理、IDE、Codex 或归属不明进程。
- 不按 `xai*`、文件扩展名或时间做宽泛邮箱/文件清理。
- push 超时先查 bridge/账号状态，再幂等重推；不能假定服务端未写入。
- 422 表示账号已隔离，优先等待或重推同一 auth，不重新注册。
- 未得到明确生产写入授权时，只运行 preflight 和本地静态验证。

## 结果格式

报告模式、脱敏邮箱、auth 路径、bridge HTTP、`account_id`、`action`、`probe`、Responses 验证和残余风险。没有 `action=created` 时，不得声称账号数量增加。
