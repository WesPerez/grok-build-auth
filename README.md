> **简体中文** | [English](README.en.md)

# grok-build-auth

面向 **x.ai / Grok 公开 Web 认证链路** 的协议研究客户端：用纯 HTTP 复现  
`注册 → SSO → OAuth PKCE（Grok Build / CLI scope）→ 导出本地 auth JSON`  
整条链路，便于协议分析、互操作性研究与本地集成测试。

默认路径不依赖浏览器。Turnstile 通过兼容 createTask 协议的打码服务完成。

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![Use](https://img.shields.io/badge/use-research%20%2F%20authorized%20only-red)](#法律边界)

---

> [!CAUTION]
> **使用本项目即视为同意 [`NOTICE`](NOTICE) 的全部条款。**  
> 项目按 **AS IS** 提供、无任何担保、维护者不负任何责任。  
> **仅限**：你拥有的系统 / 合法 CTF / 授权 bug bounty in-scope 资产 / 安全研究与教学。  
> **严禁**：欺诈、批量造号转售、黑产代注册、未授权目标、故意违反第三方 ToS。  
> 一切法律责任由使用者自负。不接受条款就**不要使用、不要 clone、删除全部副本**。

---

## 法律边界

| | 说明 |
|---|---|
| **允许** | 自有账号与本地环境；明确授权范围内的安全研究；CTF / 课堂 / 学术协议研究；离线阅读源码 |
| **禁止** | 欺诈滥用、批量造号转售、代注册牟利、未授权自动化攻击、规避平台安全机制用于非法目的 |
| **责任** | 账号封禁、额度损失、民事 / 刑事 / 行政责任等全部由**使用者**承担 |
| **关系** | 与 xAI、Grok、Cloudflare、CLIProxyAPI、任何打码 / 邮箱服务商**无隶属、无授权、无赞助** |

完整条款见 [`NOTICE`](NOTICE)。License 是 [MIT](LICENSE)，但 **MIT 不是免责的全部**。

不确定是否合法 —— **不要运行**。先问律师，或先联系目标平台安全团队。

---

## 这是什么

研究型协议客户端，不是官方 SDK。主要能力：

| 阶段 | 内容 |
|---|---|
| **注册** | `accounts.x.ai` 邮箱验证码（gRPC-web）+ Turnstile + Next.js Server Action 建号 |
| **SSO** | 从建号响应 / set-cookie 链提取 session JWT，供 OAuth 复用 |
| **OAuth** | `auth.x.ai` PKCE + CookieSetter + consent；失败时再走 CreateSession |
| **导出** | 写出与 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) 兼容的本地 `type=xai` auth 文件（Grok Build 通道） |

值得看的点：

- **协议优先**：默认纯 HTTP（`curl_cffi` 指纹会话），不启浏览器
- **SSO 复用**：注册 session 可跳过 OAuth 二次打码（快路径）
- **互操作导出**：`cli-chat-proxy.grok.com` + grok-cli headers，**不是** `api.x.ai` 计费 API 密钥通道
- **并发注册**：注册可多线程；OAuth 默认串行，降低会话冲突

---

## 架构

```mermaid
flowchart LR
    A[run.py] --> B[注册 client.py<br/>邮箱 + Turnstile]
    B --> C[SSO sso.py]
    C --> D[OAuth oauth_protocol.py<br/>PKCE + consent]
    D --> E[token 交换]
    E --> F[cliproxyapi_auth/*.json<br/>CLIProxyAPI 可加载]
```

SSO **不能**单独变成 CPA auth 文件；必须完成 OAuth 拿到 `access_token` / `refresh_token` 后才能导出。

---

## 现状与门槛

这不是「零配置即用」的产品。至少需要：

- Python 3.10+（服务器批量流程使用 Python 3.12 验证）
- YesCaptcha（或兼容 createTask 协议）的 API key，用于 Turnstile
- 临时邮箱：Tempmail.lol API key，**或**你自建的 Cloudflare D1 别名邮箱
- （可选）HTTP(S) 代理
- （可选）本地已安装的 CLIProxyAPI，用于加载导出的 auth 目录

平台条款、风控、接口变更会导致链路随时失效；维护者**无义务**持续适配。

---

## 上手

Web 控制台支持两种真实注册后端：

- `protocol-yescaptcha`：协议注册 + YesCaptcha ProxylessM1，支持有界并发。
- `browser-playwright-edge`：Playwright + 系统 Edge 完成整个注册页流程，固定 1 路并发。

新 auth 在导入前会逐个请求 Grok CLI Responses，导入后再通过
Sub2API Grok 分组做真实请求。探针失败的账号不会进入生产调度池。
代理池配置参考 `private.example/proxies.example.json`。未配置节点池时，
继续把 `runtime.env` 中现有 `HTTPS_PROXY/HTTP_PROXY` 作为单一 sticky 出口。
运维与健康门禁见 [`PROXY_POOL_OPERATIONS.zh-CN.md`](PROXY_POOL_OPERATIONS.zh-CN.md)，
外部项目与论坛调研依据见
[`docs/research/GROK_RESEARCH_AND_IMPROVEMENTS.zh-CN.md`](docs/research/GROK_RESEARCH_AND_IMPROVEMENTS.zh-CN.md)。

### 安装

```bash
git clone https://github.com/<you>/grok-build-auth.git
cd grok-build-auth
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
# source .venv/bin/activate

pip install -r requirements-lock.txt
cp .env.example .env
# 编辑 .env：只填你自己的密钥，切勿提交 .env
```

### 配置

| 变量 | 必须 | 说明 |
|---|---|---|
| `YESCAPTCHA_API_KEY` | 是 | Turnstile 打码 |
| `TEMPMAIL_API_KEY` | `-e tempmail` 时 | 临时邮箱 |
| `CLOUDFLARE_API_TOKEN` | `-e cloudflare` 时 | CF API token |
| `CLOUDFLARE_ACCOUNT_ID` | 同上 | CF 账户 |
| `CLOUDFLARE_D1_DB_ID` | 同上 | D1 库 ID |
| `ALIAS_MAIL_DOMAINS` | 同上 | 你控制的邮箱域名（逗号分隔） |
| `IMAP_SERVER` / `IMAP_USERNAME` / `IMAP_PASSWORD` / `IMAP_EMAIL` | `-e imap` 时 | 自建 IMAP 邮箱 |
| `CLIPROXYAPI_AUTH_DIR` | 否 | 默认 `./cliproxyapi_auth` |
| `HTTPS_PROXY` / `HTTP_PROXY` | 否 | 代理 |

**永远不要**把 `.env`、`private/` 或 token 目录提交进 Git。服务器批量部署见
[`SERVER_DEPLOYMENT.zh-CN.md`](SERVER_DEPLOYMENT.zh-CN.md)，私有目录初始化见
[`private.example/README.md`](private.example/README.md)。Sub2API 的 Grok 分组、
Responses 路由、CLI 请求头兼容代理、验证和回滚见
[`SUB2API_GROK_RESPONSES.zh-CN.md`](SUB2API_GROK_RESPONSES.zh-CN.md)。

服务器日常操作推荐使用本机网页控制台：

```bash
bash start_web_console.sh
```

本机可打开 `http://127.0.0.1:17860`；配置 Nginx 后推荐访问
`https://weesai.com/grok/`。页面提供环境检查、
新建批次、实时进度、逐账号状态、日志、历史记录和失败导入续跑。

### 运行（研究 / 自有账号场景）

```bash
# 单次完整链路：注册 + SSO + OAuth + 导出
python run.py -n 1

# 自建 Cloudflare 邮箱后端
python run.py -n 1 -e cloudflare

# 自建 IMAP 邮箱后端
python run.py -n 1 -e imap

# 仅注册 + SSO（不导出 CPA auth）
python run.py -n 1 --no-oauth

# 指定 CLIProxyAPI auth 目录
python run.py -n 1 --cliproxyapi-auth-dir /path/to/CLIProxyAPI/data/auth

# 将机器结果写入 0600 JSON，失败返回非零退出码
python run.py -n 1 --result-json /secure/path/result.json
```

### 辅助脚本

```bash
# 已有账号：单独走 OAuth
python xai_oauth_login.py

# 把 oauth_output 记录导出为 CPA auth
python xai_oauth_export_cliproxyapi.py --cliproxyapi-auth-dir ./cliproxyapi_auth

# 探测 Build 用量信号（不打印完整 token）
python xai_build_quota_probe.py --auth-dir ./cliproxyapi_auth
```

### 导出文件形态（本地文件，非官方密钥）

```json
{
  "type": "xai",
  "auth_kind": "oauth",
  "access_token": "...",
  "refresh_token": "...",
  "base_url": "https://cli-chat-proxy.grok.com/v1",
  "headers": {
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-grok-client-version": "0.2.93",
    "x-grok-client-identifier": "grok-shell"
  }
}
```

将 CLIProxyAPI 的 `auth-dir` 指向该目录后按 CPA 文档热加载即可（仅限合法自用场景）。

---

## 协议概要

**注册**

1. Warm-up + 动态抓取 Next.js action  
2. 邮箱验证码（gRPC-web）  
3. Turnstile  
4. 建号 + 提取 SSO  

**Build OAuth**

1. PKCE authorize  
2. 有 SSO：CookieSetter + consent（通常无需二次打码）  
3. 无 SSO：Turnstile + CreateSession，再 consent  
4. code → token → 写本地 auth JSON  

接口与额度策略以平台实时行为为准，文档数值仅供研究参考。

---

## 目录结构

```text
.
├── NOTICE                         # 具有约束力的使用须知（必读）
├── LICENSE                        # MIT
├── README.md / README.en.md
├── SUB2API_GROK_RESPONSES.zh-CN.md # Sub2API Grok Responses 接入与兼容代理
├── SECURITY.md
├── run.py                         # 主入口
├── register_and_import.sh         # 服务器批量包装器
├── scripts/register_and_import.py # 批量注册、导入和中断恢复编排器
├── private.example/               # 私有配置空模板
├── xai_oauth_login.py
├── xai_oauth_export_cliproxyapi.py
├── xai_build_quota_probe.py
├── requirements.txt
├── .env.example
├── xconsole_client/               # 协议库（Python 包名，历史命名）
│   ├── client.py                  # 注册
│   ├── oauth_protocol.py          # 纯协议 OAuth
│   ├── xai_oauth.py               # PKCE / 导出 / 回退
│   └── sso.py / solver.py / ...
└── alias_mail/                    # 可选：Cloudflare 邮箱助手
```


服务器运行产物集中保存在已忽略的 `private/`；旧输出目录仍保持 gitignore。

---

## 已知限制

- 依赖第三方公开接口，**随时可能因部署变更而失效**
- Turnstile / 邮箱服务稳定性影响成功率与耗时
- 服务器版 `run.py` 固定为单账号单线程；批量由外层脚本顺序编排
- SSO alone ≠ CPA auth；必须完成 OAuth
- Playwright 回退为可选依赖，默认协议路径不需要

---

## 贡献

欢迎在**合法研究与授权场景**下贡献：

1. 协议变更后的适配（附抓包对比 / 复现步骤）
2. 文档与翻译完善
3. 测试与健壮性（超时、重试、错误分类）
4. 脱敏后的研究笔记（禁止提交真实 token / 邮箱 / cookie）

**不接受**意图用于未授权滥用、批量黑产、绕过平台安全策略的 PR / Issue。

安全问题请走私密渠道，见 [`SECURITY.md`](SECURITY.md)。

---

## 社区

| 渠道 | 用途 |
|---|---|
| [**LINUX DO**](https://linux.do/) | 技术讨论、协议研究反馈、长期记录 |
| QQ 群 **`1058789350`** | 中文圈实时交流 |
| GitHub Issues | bug 报告与 PR（主入口） |

---

## 致谢

- [curl_cffi](https://github.com/lexiforest/curl_cffi) — TLS / HTTP2 指纹会话  
- 相关公开 Web 标准：OAuth 2.0、PKCE、gRPC-web  

---

## 免责声明

> [!IMPORTANT]
> **使用本项目即视为你已完整阅读、完全理解、并明确接受 [`NOTICE`](NOTICE) 的全部条款。**  
> 不能接受 —— 不要使用本项目，删除所有副本。

**摘要（完整文本以 NOTICE 为准）：**

1. **AS IS**：无适销性、特定用途、持续兼容等任何担保。  
2. **仅限授权范围**：自有系统 / 合法 CTF / 授权研究；禁止欺诈、批量转售、未授权目标。  
3. **责任自负**：含账号封禁、民事 / 刑事 / 行政责任、第三方索赔等。  
4. **维护者无义务**回复 issue、修 bug、做协议适配或提供支持。  
5. **无隶属关系**：不代表 xAI、Grok、Cloudflare、CLIProxyAPI 或任何提及的第三方。  

License：[MIT](LICENSE) · 使用须知：[NOTICE](NOTICE) · 安全：[SECURITY.md](SECURITY.md)
