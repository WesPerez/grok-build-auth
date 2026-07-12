# GROKAUTH

GROKAUTH 用纯 HTTP 或受控浏览器流程完成：

```text
x.ai 注册 -> SSO -> Grok Build OAuth -> auth JSON
          -> 上游探针 -> Sub2API 导入 -> 分组真实请求验证
```

它是研究与自有环境集成工具，不是 xAI 官方 SDK。只允许用于自有账号、明确授权的安全研究、教学或测试环境。禁止欺诈、转售、未授权批量注册和规避第三方安全策略。使用前必须阅读 [NOTICE](NOTICE) 和 [SECURITY.md](SECURITY.md)。

## 从哪里开始

本项目只保留两个操作入口：

- 本页：理解项目、完成本地单账号运行。
- [完整操作手册](OPERATIONS.zh-CN.md)：服务器批次、外部客户端、bridge、代理池、Sub2API、429 轮询、恢复和回滚。

生产环境优先使用服务器编排器。只有必须使用外部机器的浏览器或出口时，才使用外部客户端 + bridge。

| 目标 | 入口 | 最终成功标准 |
|---|---|---|
| 本地生成 auth JSON | `python run.py -n 1` | OAuth auth 文件存在 |
| 服务器生成可用 Sub2API 账号 | `scripts/register_and_import.py` | manifest 为 `imported-preprobed` |
| 外部客户端生成可用 Sub2API 账号 | `grok_register_ttk.py` + bridge | bridge 返回 `probe=passed`，且分组 Responses 请求成功 |

Web 注册成功、拿到 SSO、写出本地账号文本，都不等于账号已经可以被 Sub2API 调度。最终目标始终是：Sub2API 中存在一个 `active + schedulable` 的 Grok OAuth 账号，并通过真实 `/v1/responses` 请求。

## 核心能力

- 邮箱验证码、Turnstile 和 x.ai 注册协议。
- 注册会话 SSO 提取。
- Grok Build OAuth PKCE，导出 CLIProxyAPI/xAI 兼容 auth JSON。
- `protocol-yescaptcha` 有界并发注册。
- `browser-playwright-edge` 单路浏览器 canary。
- Mailu/IMAP 邮箱创建、批次产物和失败恢复。
- 导入前单 auth 上游探针、Sub2API 幂等导入、精确账号收口和导入后分组探针。
- 可选代理池健康门禁和注册阶段 sticky 出口。

Grok Build OAuth 使用 `https://cli-chat-proxy.grok.com/v1`，不是 `https://api.x.ai/v1` 的付费 API Key 通道。SSO cookie 不能替代 OAuth `access_token` 和 `refresh_token`。

## 本地快速上手

要求 Python 3.10+；当前生产流程使用 Python 3.12 验证。

```bash
git clone https://github.com/<you>/grok-build-auth.git
cd grok-build-auth
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-lock.txt
cp .env.example .env
chmod 600 .env
```

按所选邮箱后端填写 `.env`：

| 变量 | 何时需要 | 说明 |
|---|---|---|
| `YESCAPTCHA_API_KEY` | 协议注册 | YesCaptcha 或兼容 createTask 服务 |
| `TEMPMAIL_API_KEY` | `-e tempmail` | Tempmail.lol API key |
| `CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID`、`CLOUDFLARE_D1_DB_ID`、`ALIAS_MAIL_DOMAINS` | `-e cloudflare` | 自有 Cloudflare D1 别名邮箱 |
| `IMAP_SERVER`、`IMAP_USERNAME`、`IMAP_PASSWORD`、`IMAP_EMAIL` | `-e imap` | 自有 IMAP 邮箱 |
| `HTTPS_PROXY`、`HTTP_PROXY` | 可选 | 本次注册和 OAuth 使用的代理 |
| `CLIPROXYAPI_AUTH_DIR` | 可选 | auth 输出目录 |

运行：

```bash
# 默认临时邮箱：注册 + SSO + OAuth + auth JSON
python run.py -n 1

# 自建 Cloudflare 邮箱
python run.py -n 1 -e cloudflare

# 自建 IMAP 邮箱
python run.py -n 1 -e imap

# 仅注册和 SSO，不生成可导入 auth
python run.py -n 1 --no-oauth

# 机器可读结果；失败返回非零退出码
python run.py -n 1 --result-json /secure/path/result.json
```

服务器版 `run.py` 固定单账号、单线程。批量和并发由外层编排器负责。

## 服务器最短路径

完整配置见 [完整操作手册](OPERATIONS.zh-CN.md)。配置完成后：

```bash
python3 scripts/register_and_import.py \
  --count 1 \
  --workers 1 \
  --registration-backend protocol-yescaptcha \
  --confirm-production-write
```

或启动只监听本机回环地址的 Web 控制台：

```bash
bash start_web_console.sh --host 127.0.0.1 --port 17860
```

## auth 文件

生成文件包含 OAuth 凭据，必须按秘密管理。典型字段为：

```json
{
  "type": "xai",
  "auth_kind": "oauth",
  "access_token": "<secret>",
  "refresh_token": "<secret>",
  "base_url": "https://cli-chat-proxy.grok.com/v1",
  "headers": {
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-grok-client-version": "<supported-version>",
    "x-grok-client-identifier": "grok-shell"
  }
}
```

OAuth access token 的实测生命周期约为 `21600` 秒，即 6 小时；refresh token 用于续期。它与免费额度恢复窗口是两件事。

## 辅助命令

```bash
# 已有账号单独完成 OAuth
python xai_oauth_login.py

# 导出已有 OAuth 记录
python xai_oauth_export_cliproxyapi.py --cliproxyapi-auth-dir ./cliproxyapi_auth

# 探测 Grok Build 额度信号，不打印完整 token
python xai_build_quota_probe.py --auth-dir ./cliproxyapi_auth

# 运行测试
pytest -q
```

## 目录

```text
.
├── README.md                    # 快速入口
├── OPERATIONS.zh-CN.md          # 唯一完整操作手册
├── NOTICE / SECURITY.md
├── run.py                       # 单账号注册、SSO、OAuth
├── scripts/register_and_import.py
├── scripts/recover_batch_oauth.py
├── web_console.py
├── clients/windows/              # 脱敏后的 Windows 浏览器客户端
├── bridge/                       # 环境变量驱动的 bridge 源码
├── skills/grok-sub2api-ops/      # 可复用 Codex 操作技能
├── private.example/             # 无秘密模板
├── xconsole_client/             # 协议与后端实现
└── docs/research/               # 研究记录，不作为生产操作入口
```

`private/`、`.env`、auth JSON、账号结果、bundle、日志和数据库备份不得提交 Git。

## 已知限制

- 第三方页面、OAuth、Turnstile 和邮箱接口变化都可能使流程失效。
- 浏览器后端是单路 canary，不应在未知出口上自动高并发轮换。
- Windows 客户端和 bridge 已纳入本仓库；现有生产服务迁移到仓库路径前，旧部署目录仍需保留。
- 历史 bridge 密钥若曾硬编码或进入聊天/日志，仍必须轮换，详见完整手册。
- `/v1/responses` 已验证；不能据此推断 Chat、图片、视频或其他端点也受支持。

License: [MIT](LICENSE)。使用条款以 [NOTICE](NOTICE) 为准。
