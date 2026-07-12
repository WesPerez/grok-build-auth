# GROKAUTH 完整操作手册

本文是服务器注册、外部客户端、bridge、代理池和 Sub2API 的唯一操作手册。示例中的 `<...>` 都必须替换为本机值；密钥只允许保存在权限为 `0600` 的私有配置或 secret manager 中。

## 1. 最终目标和路径选择

最终成功不是“注册页面显示成功”，而是 Sub2API 中生成一个可用账号：

- `platform=grok`
- `type=oauth`
- `status=active`
- `schedulable=true`
- 只绑定 Grok 分组
- `credentials.base_url=http://grok-cli-proxy:8080/v1`
- 指定账号探针和 Grok 分组 `/v1/responses` 均成功

有两条生产路径：

```text
服务器路径（推荐）
GROKAUTH -> Mailu/IMAP -> x.ai 注册 -> OAuth
         -> 单 auth preprobe -> 备份/导入/收口 -> group postprobe

外部客户端路径
grok_register_ttk.py -> bridge 邮箱 API -> x.ai 注册 -> OAuth
                     -> CPA JSON push -> bridge 隔离/探针/入组 -> Sub2API
```

| 场景 | 选择 |
|---|---|
| 服务器已有 Mailu、YesCaptcha、Sub2API | 服务器路径 |
| 需要外部 Windows 浏览器或外部网络出口 | 外部客户端路径 |
| 只研究协议或生成本地 auth | `run.py` 单账号路径 |

服务器编排有更完整的备份、token-hash 幂等、manifest 和双探针，日常生产优先使用它。

## 2. 组件和信任边界

| 组件 | 当前职责 | 代码位置 |
|---|---|---|
| GROKAUTH | 注册、OAuth、批次、探针、导入编排 | 本仓库 |
| 外部客户端 | 浏览器注册、OAuth、CPA JSON 推送 | 独立目录/部署包 `grok-register` |
| bridge | 邮箱兼容 API、CPA 接收、隔离探针和入组 | 独立部署 `grok-register-bridge` |
| Sub2API | 账号池、分组、轮询和 OpenAI 兼容 API | 独立仓库/部署 |
| `grok-cli-proxy` | 固定 Grok CLI 上游并补齐兼容请求头 | Sub2API 部署侧 sidecar |

外部客户端和 bridge 目前不是本 Git 仓库的一部分。部署时必须分别取得对应代码，不能只 clone GROKAUTH 就假定外部链路存在。

## 3. 共同前置条件

- Python 3.12 为当前生产验证版本。
- Docker CLI，以及可访问的 Mailu、PostgreSQL 和 Sub2API。
- YesCaptcha 或兼容 createTask 的 Turnstile 服务。
- 自有、授权且稳定的 HTTP/SOCKS 代理；代理失败时不得静默回退直连。
- Sub2API 中已有独立 Grok 分组、可用 Grok API Key 和 `grok-cli-proxy`。
- 所有系统时钟正确，否则 JWT、OAuth 和冷却时间会误判。

安装 GROKAUTH：

```bash
cd /path/to/grok-build-auth
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-lock.txt
```

## 4. 服务器路径

### 4.1 初始化私有目录

```bash
cd /path/to/grok-build-auth
mkdir -p private
chmod 700 private
cp private.example/runtime.env.example private/runtime.env
cp private.example/SECRETS.example.md private/SECRETS.md
chmod 600 private/runtime.env private/SECRETS.md
```

每个批次位于 `private/runs/<batch-id>/`：

```text
manifest.json
auth/<mailbox>/...
results/<localpart>.json
logs/<localpart>.log
bundle/sub2api-bundle.json
bundle/pending-sub2api-bundle.json
import/helper.log
import/result.json
backup/*.dump
```

`results` 含账号密码，`auth` 和 `bundle` 含 OAuth token，日志和备份也按敏感数据处理。

### 4.2 `private/runtime.env`

通用必填：

| 变量 | 说明 |
|---|---|
| `YESCAPTCHA_API_KEY` | 协议注册 Turnstile 服务密钥 |
| `IMAP_SERVER`、`IMAP_PORT`、`IMAP_SSL` | Mailu/IMAP 地址 |
| `IMAP_PASSWORD` | 批次邮箱统一密码，由进程环境注入 |
| `MAILU_DOMAIN` | 批次邮箱域名 |
| `MAILU_DB` | Mailu SQLite 文件，用于精确确认邮箱记录 |
| `MAILU_ADMIN_CONTAINER`、`MAILU_FLASK_BIN` | Mailu 官方用户创建/删除入口 |
| `MAILU_IMAP_CONTAINER`、`MAILU_MAIL_ROOT` | 仅用于精确清理本批失败邮箱 |
| `SUB2API_GROUP` | 必须为 `grok` |
| `GROK_ACCOUNT_BASE_URL` | 必须为 `http://grok-cli-proxy:8080/v1` |

生产导入额外必填：

| 变量 | 说明 |
|---|---|
| `SUB2API_ENV` | Sub2API 部署的私有环境文件 |
| `SUB2API_URL` | 本机管理/API 地址，推荐 loopback |
| `SUB2API_POSTGRES_CONTAINER` | PostgreSQL 容器 |
| `SUB2API_PG_USER`、`SUB2API_PG_DB` | 数据库用户和库名 |
| `SUB2API_IMPORT_TOOL` | `sub2api_live_tool.py` 的绝对路径 |

调度和探针配置：

```dotenv
GROK_MAX_REGISTRATION_WORKERS=2
GROK_PREIMPORT_PROBE_MAX_WAIT_SECONDS=900
GROK_PREIMPORT_PROBE_RETRY_INTERVAL_SECONDS=60
GROK_PROXY_POOL_FILE=
GROK_ALLOW_MISSING_SUB2API_PROXY_IDS=false
GROK_PROXY_HEALTH_ATTEMPTS=3
GROK_PROXY_HEALTH_TIMEOUT=10
GROK_BROWSER_PROXY_URL=
GROK_BROWSER_HEADED=true
```

没有代理池时，`HTTPS_PROXY`/`HTTP_PROXY` 作为单一 sticky 出口。两者都为空会直连，生产使用前必须明确接受这个结果。

启用代理池时应保持 `GROK_BROWSER_PROXY_URL` 为空，让每个 attempt 的 lease 代理生效。非空全局浏览器代理会覆盖真实浏览器出口，但 manifest 和导入后 ProxyID 仍可能记录 lease，造成出口不一致。

### 4.3 代理池

复制示例：

```bash
cp private.example/proxies.example.json private/proxies.json
chmod 600 private/proxies.json
```

推荐节点结构：

```json
{
  "version": 1,
  "proxies": [
    {
      "ref": "node-01",
      "url_env": "GROK_PROXY_NODE_1",
      "enabled": true,
      "max_active_leases": 1,
      "sub2api_proxy_id": 12
    }
  ]
}
```

实际 URL 写入 `runtime.env`：

```dotenv
GROK_PROXY_POOL_FILE=/absolute/path/private/proxies.json
GROK_PROXY_NODE_1=socks5://127.0.0.1:<port>
```

支持 `http`、`https`、`socks5`、`socks5h`。每个启用节点默认必须有有效的 `sub2api_proxy_id`，这样注册、OAuth、preprobe 和导入后的业务请求才能保持同一代理绑定。

只有明确接受导入后不保证同出口时，才设置：

```dotenv
GROK_ALLOW_MISSING_SUB2API_PROXY_IDS=true
```

此时 manifest 会记录 `postimport_stickiness=false`。不能再宣称账号全生命周期 sticky。

运行中立健康检查：

```bash
python3 scripts/check_proxy_pool.py \
  --private-dir /path/to/grok-build-auth/private \
  --attempts 3 \
  --timeout 10
```

任一节点成功率低于 2/3、TLS 校验失败、出口漂移或不同 ref 实际同出口，整批 fail closed。不要把论坛节点凭据、完整代理 URL 或出口 IP 写进仓库和公开日志。

### 4.4 运行服务器流程

推荐单账号 canary：

```bash
python3 scripts/register_and_import.py \
  --count 1 \
  --workers 1 \
  --registration-backend protocol-yescaptcha \
  --confirm-production-write
```

批量时逐步提高 `--count` 和 `--workers`。`--count` 是尝试数，不保证全部成功。浏览器后端必须单路：

```bash
python3 scripts/register_and_import.py \
  --count 1 \
  --workers 1 \
  --registration-backend browser-playwright-edge \
  --confirm-production-write
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--failure-policy continue|abort` | 默认继续；可在首次失败时终止 |
| `--max-consecutive-failures N` | 默认连续 3 次失败停止创建新 attempt |
| `--import-partial` | 明确允许只导入注册和探针通过的子集 |
| `--cleanup-failed-mailboxes` | 仅精确删除本批且确认上游账号未创建的邮箱 |
| `--no-import` | 真实注册并生成 bundle，但不写 Sub2API；不是 dry-run |
| `--resume <batch-id>` | 复用已有 auth 续跑导入，不重新注册 |
| `--confirm-production-write` | 生产写入硬门禁 |

批次中存在注册失败时，默认不导入成功子集。只有审查失败原因后才使用 `--import-partial`。

### 4.5 Web 控制台

```bash
bash start_web_console.sh --host 127.0.0.1 --port 17860
```

控制台必须只监听 loopback。若通过 Nginx 暴露，必须使用 Basic Auth 或等效强认证，并保留长请求超时。不要匿名公开生产操作接口。

控制台支持环境检查、创建批次、实时日志、历史批次和导入续跑；当前不提供 OAuth recovery 和 `--no-import` UI，这两项使用 CLI。

### 4.6 真实执行顺序

1. 获取跨进程批次锁。
2. 校验 `runtime.env` 权限、必填字段、worker、目标分组和 base URL。
3. 如启用代理池，校验 ProxyID、TLS、成功率、出口稳定性和重复出口。
4. 创建唯一 Mailu 邮箱。
5. 每个 attempt 独立运行 `run.py`，完成注册、SSO、OAuth 和 auth JSON。
6. 校验结果邮箱、auth 路径边界、凭据完整性并聚合 bundle。
7. 对每个 auth 直接请求 Grok CLI `/responses` 做 preprobe。
8. 403、传输错误和 5xx 可按配置等待重试；429 不自动重试。
9. 按 access token hash 查询已存在账号，只导入缺失项；写入前创建数据库恢复点。
10. 对本批精确账号统一收口 Grok 分组、sidecar base URL、凭据、调度状态和可选 ProxyID。
11. 验证数据库精确状态。
12. 使用 Grok 分组 API Key 调用 Sub2API `/v1/responses` 做 postprobe。
13. 全部成功后 manifest 进入 `imported-preprobed`。

新账号资格可能需要传播。preprobe 首次 403、等待后变 200，不代表 token 只有几分钟有效；默认最多等待 900 秒，每 60 秒重试。

### 4.7 成功判定

不要只看进程退出码。检查：

```bash
jq '{status, imported_ids, preimport_auth_probes, exact_account_state, postimport_group_probe}' \
  private/runs/<batch-id>/manifest.json
```

必须满足：

- `status=imported-preprobed`
- 预探针通过数量等于实际导入 auth 数量
- `imported_ids` 数量匹配
- `exact_account_state` 通过平台、类型、状态、分组、base URL、凭据和代理检查
- `postimport_group_probe.status=200`
- postprobe 为 completed 且输出匹配

### 4.8 失败恢复

导入失败或仅注册未导入：

```bash
python3 scripts/register_and_import.py \
  --resume <batch-id> \
  --confirm-production-write
```

它会校验 bundle hash 和 auth 路径，按 token hash 复用已有账号，只导入缺失项。每次新的 import/reconcile 尝试会基于当时数据库状态再创建恢复点。

已建号但 OAuth 失败：

```bash
python3 scripts/recover_batch_oauth.py \
  --batch <batch-id> \
  --attempts 3

python3 scripts/register_and_import.py \
  --resume <batch-id> \
  --import-partial \
  --confirm-production-write
```

`preimport-probe-failed` 不能靠无脑 resume 解决。先判断是资格传播、token、代理、403 entitlement 还是 429 额度，再决定等待、补 OAuth 或隔离账号。

## 5. 外部客户端路径

### 5.1 部署边界和要求

外部客户端代码位于独立的 `grok-register` 项目。要求：

- Python 3.12 或 3.13。
- Windows 使用本机 Chrome/Chromium；Linux 使用 Microsoft Edge，并提供 DISPLAY/Xvfb。
- 客户端本机有真实可用的 HTTP/SOCKS 代理。
- 能通过 HTTPS 访问 bridge 公网入口。

安装：

```bash
cd /path/to/grok-register
python -m pip install -r requirements.txt
```

先用 1 路并发完成全链验收，再考虑提高到 2。旧的 5 到 15 路建议不适用于当前长探针和不稳定免费出口。

### 5.2 `config.json`

最小生产配置：

```json
{
  "email_provider": "cloudflare",
  "cloudflare_api_base": "https://<bridge-domain>",
  "cloudflare_path_accounts": "/admin/new_address",
  "cloudflare_path_domains": "/api/domains",
  "cloudflare_path_token": "/api/token",
  "cloudflare_path_messages": "/api/mails",
  "cloudflare_auth_mode": "bearer",
  "cloudflare_api_key": "<bridge-management-secret>",
  "defaultDomains": "<mail-domain>",

  "proxy": "<client-local-proxy-url>",
  "register_count": 1,
  "hide_window": false,
  "block_media_fonts": false,

  "cpa_export_enabled": true,
  "cpa_auth_dir": "./cpa_auths",
  "cpa_base_url": "https://cli-chat-proxy.grok.com/v1",
  "cpa_push_enabled": true,
  "cpa_remote_base": "https://<bridge-domain>",
  "cpa_remote_secret": "<bridge-management-secret>",
  "cpa_remote_verify_tls": true,
  "cpa_push_proxy": "",
  "cpa_push_required": true,
  "cpa_push_timeout_sec": 240,

  "mint_proxy": "",
  "mint_timeout_sec": 300,
  "mint_required": true
}
```

注意：

- `proxy` 必须是客户端本机实际监听地址，不能照抄服务器的 `127.0.0.1:<port>`。
- `mint_proxy` 为空时复用注册代理。
- 邮箱 API 和 CPA push 当前可使用同一 bridge 管理凭据，但应长期拆分权限。
- `config.json` 含管理密钥，权限必须为 `0600`，不得打包分享或提交 Git。
- `mint_timeout_sec` 应覆盖 device flow 和协议 fallback；60 秒在网络波动时偏紧。

### 5.3 运行

Windows：

```powershell
cd D:\path\to\grok-register
python grok_register_ttk.py
```

按提示输入并发数 `1`。

Linux/Xvfb 示例：

```bash
cd /path/to/grok-register
DISPLAY=:99 bash -c 'echo 1 | .venv/bin/python3 grok_register_ttk.py'
```

脚本没有稳定的非交互参数接口，不要把位置参数当 CLI 选项使用。

### 5.4 客户端内部流程

1. 通过 bridge 创建 Mailu 邮箱并取得 1 小时邮箱 JWT。
2. 浏览器通过客户端代理完成 x.ai 注册和邮箱验证码。
3. 保存账号和 SSO。
4. 尝试 device OAuth；失败时复用 SSO 走协议 OAuth。
5. 要求同时得到 access token 和 refresh token。
6. 写出 `cpa_auths/xai-<email>.json`。
7. 将同一 JSON push 到 bridge。
8. bridge 先隔离账号、执行指定账号探针，通过后才进入 Grok 分组。

### 5.5 Bridge HTTP 契约

邮箱创建：

```http
POST /admin/new_address
Authorization: Bearer <bridge-management-secret>
Content-Type: application/json

{"name":"<random-localpart>","domain":"<mail-domain>"}
```

成功返回 `address` 和邮箱 JWT。邮件读取使用邮箱 JWT，不是管理凭据：

```http
GET /api/mails
Authorization: Bearer <mailbox-jwt>

GET /api/mail/<message-id>
Authorization: Bearer <mailbox-jwt>
```

CPA push：

```http
POST /v0/management/auth-files?name=xai-<email>.json
X-Management-Key: <bridge-management-secret>
Content-Type: application/json

<CPA xAI auth JSON>
```

请求体最大 1 MiB。状态含义：

| HTTP | 含义 |
|---|---|
| 200 | 返回 `status=ok`、`account_id`、`probe=passed`，账号已通过指定账号探针并入组 |
| 400 | email/token 结构不完整、token 临近过期或缺 `sub` |
| 401 | 邮箱 API Bearer 错误或邮箱 JWT 失效 |
| 403 | CPA 管理密钥错误 |
| 413 | 请求体超限 |
| 422 | 账号在探针窗口内未通过，保持无分组且不可调度 |
| 500 | Sub2API 创建、更新、查询或服务端依赖失败 |

### 5.6 Bridge 的隔离、探针和入组

当前 bridge 按邮箱生成稳定账号名。收到 auth 后：

1. 校验 email、access token、refresh token、JWT `exp` 和 `sub`。
2. 创建或更新账号时先设置 `group_ids=[]`、`schedulable=false`。
3. 强制写入 `http://grok-cli-proxy:8080/v1`。
4. 调用 Sub2API 指定账号 Responses test。
5. 每 30 秒重试，最长 180 秒。
6. 通过后绑定 Grok 分组并设置 `schedulable=true`。
7. 返回 `probe=passed`。

同邮箱重推会更新同一账号，适合 OAuth token 更新和 422 后恢复。注意：更新已有可用账号时会先隔离；若本次 probe 失败，它会暂时退出生产池，这是 fail closed 行为。

### 5.7 外部客户端成功判定

当前客户端存在一个已知口径缺陷：`register_one()` 没有检查 CPA export/push 的返回对象。即使 push 返回 422/500，worker 仍可能打印“注册成功”。因此本地成功计数不能作为最终判据。

必须同时确认：

- 本地 CPA JSON 已生成且权限安全。
- 日志明确显示 push HTTP 200。
- bridge 响应含 `account_id` 和 `probe=passed`。
- Sub2API 账号为 active、schedulable、唯一 Grok 分组、内网 sidecar base URL。
- Grok 分组 `/v1/responses` 返回 200。

### 5.8 外部客户端失败恢复

- 邮箱 API 401：检查 `cloudflare_auth_mode=bearer` 和管理凭据。
- 验证码失败：客户端最多更换邮箱重试 3 次。
- OAuth 失败：保留邮箱、密码和 SSO，从已有账号补 OAuth，不要重新建号。
- push 422：账号已隔离。等待资格传播或上游恢复后，重推同一个 CPA JSON。
- push 超时：先查 bridge 日志和 Sub2API 账号，不能假定服务端没有写入；确认后再幂等重推。
- push 403：核对 management secret，不要把密钥放到命令行历史。
- push 500：查看 bridge journal、Sub2API 和 PostgreSQL；不要直接重复注册新账号。

## 6. Sub2API 客户端调用

为调用方创建只绑定 Grok 分组的专用 API Key。Base URL 指向 Sub2API 公网 OpenAI 兼容入口，不是 bridge 管理入口。

模型列表：

```bash
curl -sS https://<sub2api-domain>/v1/models \
  -H 'Authorization: Bearer <grok-group-api-key>'
```

非流式 Responses：

```bash
curl -sS https://<sub2api-domain>/v1/responses \
  -H 'Authorization: Bearer <grok-group-api-key>' \
  -H 'Content-Type: application/json' \
  -d '{
    "model":"grok-4.5",
    "input":"Reply exactly: OK",
    "max_output_tokens":8,
    "store":false
  }'
```

流式 Responses：

```bash
curl -sS -N https://<sub2api-domain>/v1/responses \
  -H 'Authorization: Bearer <grok-group-api-key>' \
  -H 'Content-Type: application/json' \
  -d '{
    "model":"grok-4.5",
    "input":"Reply exactly: OK",
    "max_output_tokens":8,
    "store":false,
    "stream":true
  }'
```

验收：非流式应为 HTTP 200、`status=completed` 且有输出；流式应出现 `response.output_text.delta` 和 `response.completed`。上游可能报告实际模型 `grok-4.5-build-free`。

## 7. Sub2API Grok 路由和账号轮询

Grok CLI OAuth 请求必须经过兼容 sidecar，补齐 CLI User-Agent 和 Grok CLI headers。sidecar 应满足：

- 固定上游 `https://cli-chat-proxy.grok.com/v1`，客户端不能选择 Host。
- 透传 Authorization，但不持久化 token 和请求体。
- 不映射宿主公网端口，只在受控容器网络内提供服务。
- `/healthz` 只表示进程存活，不代表 OAuth、资格或额度可用。

若 Sub2API 需要 `XAI_ALLOW_UNSAFE_URL_OVERRIDES=true` 才能指向 sidecar，该设置扩大了管理员可配置 URL 的 SSRF 风险。只允许受信管理员修改 Grok 账号；Sub2API 原生支持 CLI headers 后应删除该变量和 sidecar。

当前已验证的 429 行为：

- 单个 Grok OAuth 账号返回 429 时，同一客户端请求会继续切换其他账号。
- `gateway.max_account_switches` 仍限制单请求最多切换数量；池内所有可选账号都失败时，客户端最终仍会收到错误。
- 优先使用 `Retry-After` 或 rate-limit reset。
- 明确出现 `included free usage` 和 `rolling 24-hour window` 时，账号冷却 24 小时。
- 普通无 reset 信息的 429 兜底冷却 15 分钟。

生产曾验证账号 A 返回 429 后，同一请求切换账号 B 并返回 HTTP 200。旧的“2 分钟”只是历史无提示 429 兜底，不是 OAuth 过期时间，也不是当前普通 429 策略。

OAuth access token 实测约 6 小时；免费额度可能按滚动 24 小时恢复。两者互不等价：

```text
token 过期 -> refresh token 续期
额度耗尽 -> 等待 reset/cooldown，期间换其他账号
```

错误判读：

| 状态 | 优先判断 |
|---|---|
| 401 | access token 过期、refresh 失败、scope 错误 |
| 402 | 不能统一解释为欠费；先检查 CLI headers、base URL、通道兼容和明确额度证据 |
| 403 | entitlement、资格传播、订阅或风控 |
| 429 | 限流/额度；看 `Retry-After`、reset 和响应体，不要立即永久禁用账号 |
| 502 | 可能是上游 402/403 的包装，也可能是 sidecar/网络错误；查 ops 日志真实状态 |

## 8. 发布、验证和回滚

推荐发布顺序：

1. 备份账号、分组、API Key 和相关数据库表。
2. 创建独立 Grok 分组和专用 Key。
3. 启动 `grok-cli-proxy` 并通过容器内 healthcheck。
4. 如需要，受控启用 URL override 并重建 Sub2API。
5. 先导入 1 个账号，保持隔离直至指定账号探针通过。
6. 校验数据库精确状态。
7. 执行非流式和流式 Responses。
8. 再逐步扩大账号数和代理节点数。

回滚前必须确认备份、目标账号和允许的中断窗口。一般顺序：停止使用 Grok 专用 Key，隔离账号，将 base URL 恢复到官方 CLI URL，等待调度缓存/outbox 收敛，再移除 sidecar 和 URL override。移除 sidecar 后若 Sub2API 仍不会原生补 CLI headers，Responses 会重新失败，因此不能只做一半回滚。

数据库恢复、删除账号、停止服务、删除镜像或清理邮箱都属于高风险操作，必须明确指定目标。只能清理由本批 manifest 精确证明归属的邮箱或产物。

## 9. 安全要求和当前安全债

永远不要提交或公开：

- `.env`、`private/`、`config.json` 中的真实秘密
- access/refresh/id token、SSO、邮箱 JWT、账号密码
- Mailu、Sub2API、bridge、Basic Auth 和代理凭据
- auth JSON、bundle、result、日志和数据库备份

Bridge 当前仍有以下已知安全债：

- 历史实现把 Mailu API token 和 Sub2API admin key 硬编码在源码中。必须迁移到 root-only EnvironmentFile/systemd credentials，并轮换旧密钥。
- access JWT 只解码 payload，未验证 xAI 签名、issuer 和 audience；真实 Sub2API probe 是最终门禁，不能移除。
- 幂等键是 email/账号名，不是 token hash 或 `sub`。
- probe 成功判断仍依赖响应文本，应改为结构化 JSON。
- 邮箱 JWT 内含 IMAP 密码，虽然签名且一小时过期，但未加密。
- CORS 为 `*`，bridge 使用标准库 WSGI server；公网入口必须由 Nginx 鉴权、限流和请求大小/超时保护。
- Web 控制台依赖反向代理认证，应用自身没有独立 CSRF/Origin 门禁。

这些债务不否定当前真实成功链路，但不能宣称 bridge 已完全硬化。

秘密一旦进入 Git 历史，先轮换/撤销，再清理历史并使用 `force-with-lease`；只删除工作区文件不够。

## 10. 日常检查清单

运行前：

- `runtime.env`、代理池和外部客户端 `config.json` 权限为 `0600`。
- Mailu、PostgreSQL、Sub2API、sidecar 和 bridge 健康。
- 代理池健康检查通过，未配置冲突的 `GROK_BROWSER_PROXY_URL`。
- Grok 分组和专用 API Key 存在。
- 从 1 个账号、1 路并发开始。

运行后：

- 服务器 manifest 为 `imported-preprobed`，或 bridge 返回 `probe=passed`。
- 指定账号测试通过。
- 分组非流式和流式 Responses 通过。
- 429 账号按 reset 正确冷却，客户端请求能切换到其他可用账号。
- 新生成的秘密产物没有进入 Git：

```bash
git status --short
git ls-files | rg '(^|/)(private|\.env)(/|$)' && exit 1 || true
```

当前节点健康、账号数量、账号 ID、生产域名、容器名和密钥轮换记录属于易变化的本地状态，应写入 gitignored 私有台账，不写死在本公开手册中。
