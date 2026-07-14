# grok-build-auth 完整操作手册

本文是服务器注册、外部客户端、bridge、代理池和 Sub2API 的唯一操作手册。示例中的 `<...>` 都必须替换为本机值；密钥只允许保存在权限为 `0600` 的私有配置或 secret manager 中。

## 1. 最终目标和路径选择

最终成功不是“注册页面显示成功”，而是 Sub2API 中生成一个可用账号：

- `platform=grok`
- `type=oauth`
- `status=active`
- `schedulable=true`
- 只绑定 Grok 分组
- `credentials.base_url=https://cli-chat-proxy.grok.com/v1`
- 指定账号探针和 Grok 分组 `/v1/responses` 均成功

有两条生产路径：

```text
服务器协议路径（可选）
grok-build-auth -> Mailu/IMAP -> x.ai 注册 -> OAuth
         -> 单 auth preprobe -> 备份/导入/收口 -> group postprobe

外部 Windows 客户端路径（常用）
grok_register_ttk.py -> bridge 邮箱 API -> x.ai 注册 -> OAuth
                     -> Sub2API auth push -> bridge 探针/导入/入组 -> Sub2API
```

| 场景 | 选择 |
|---|---|
| 明确选择纯协议、服务器邮箱和服务器代理池 | 服务器协议路径 |
| 日常批量，或需要 Windows 浏览器/外部网络出口 | 外部客户端路径 |
| 用户明确授权在部署服务器模拟客户端，且 Edge/Xvfb/代理已通过 canary | Linux/Xvfb 服务器模拟客户端 |
| 只研究协议或生成本地 auth | `run.py` 单账号路径 |

默认不在同一台服务器混跑协议编排器和浏览器客户端。用户明确授权服务器模拟客户端时，可在 Linux/Xvfb 上复用正式客户端，但不得同时运行 `register_and_import.py`；必须先单账号 canary，再使用隔离 route 和代理扩到两路。只有旧的“先 quarantine 入库、再探针筛选”流程废弃。

## 2. 组件和信任边界

| 组件 | 当前职责 | 代码位置 |
|---|---|---|
| grok-build-auth | 服务器协议注册、OAuth、批次、探针、导入编排 | 本仓库 |
| 外部客户端 | 浏览器注册、OAuth、Sub2API auth 推送 | `clients/windows/` |
| bridge | 邮箱兼容 API、Sub2API auth 接收、探针、导入和入组 | `bridge/bridge.py` |
| Sub2API | 账号池、分组、轮询、Grok CLI 请求兼容和 OpenAI 兼容 API | 独立仓库/部署 |

Windows 调试现场中的日志、真实 auth、账号密码、代理节点和一次性 patch/test 脚本没有纳入仓库。仓库只保留经过脱敏和收口的正式入口。

## 3. 共同前置条件

- Python 3.12 为当前生产验证版本。
- Docker CLI，以及可访问的 Mailu、PostgreSQL 和 Sub2API。
- YesCaptcha 或兼容 createTask 的 Turnstile 服务。
- 自有、授权且稳定的 HTTP/SOCKS 代理；代理失败时不得静默回退直连。
- Sub2API 中已有独立 Grok 分组、可用 Grok API Key，并包含原生 Grok CLI 请求兼容。
- 所有系统时钟正确，否则 JWT、OAuth 和冷却时间会误判。

安装 `grok-build-auth`：

```bash
cd /root/grok-build-auth
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-lock.txt
```

## 4. 服务器路径

### 4.1 初始化私有目录

```bash
cd /root/grok-build-auth
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
| `GROK_ACCOUNT_BASE_URL` | 必须为 `https://cli-chat-proxy.grok.com/v1` |

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
GROK_PROXY_ROTATION_STATE_FILE=
GROK_BIND_SUB2API_PROXY_AFTER_IMPORT=false
GROK_ALLOW_MISSING_SUB2API_PROXY_IDS=false
GROK_PROXY_HEALTH_ATTEMPTS=3
GROK_PROXY_HEALTH_TIMEOUT=10
GROK_BROWSER_PROXY_URL=
GROK_BROWSER_HEADED=true
```

没有代理池时，`HTTPS_PROXY`/`HTTP_PROXY` 作为注册、OAuth 和 preprobe 的单一出口。两者都为空会直连，生产使用前必须明确接受这个结果。

启用代理池时应保持 `GROK_BROWSER_PROXY_URL` 为空，让每个 attempt 的 lease 代理生效。非空全局浏览器代理会覆盖真实浏览器出口，但 manifest 仍记录 lease；若又显式开启导入后粘性，还会造成记录的 ProxyID 与真实注册出口不一致。

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
      "max_active_leases": 1
    }
  ]
}
```

实际 URL 写入 `runtime.env`：

```dotenv
GROK_PROXY_POOL_FILE=/absolute/path/private/proxies.json
GROK_PROXY_ROTATION_STATE_FILE=/absolute/path/private/proxy-rotation.json
GROK_PROXY_NODE_1=socks5://127.0.0.1:<port>
```

支持 `http`、`https`、`socks5`、`socks5h`。默认 `GROK_BIND_SUB2API_PROXY_AFTER_IMPORT=false`：代理 lease 只用于注册、OAuth 和导入前 auth preprobe，导入后 Sub2API 正常调用不依赖注册节点。轮询状态文件必须位于受限目录并保持 `0600`；它保存下一个节点，使连续运行的单账号批次也会按节点顺序轮询，而不是每次重新从 `node-01` 开始。

跨进程状态文件只持久化轮询游标；`max_active_leases` 是单进程并发上限。生产入口依靠全局 batch lock 阻止多个 `register_and_import.py` 批次同时运行，不要绕过该锁并行启动多个注册进程。

只有明确要求账号长期保持注册出口时，才设置：

```dotenv
GROK_BIND_SUB2API_PROXY_AFTER_IMPORT=true
```

此时再为每个节点添加 `sub2api_proxy_id`。该模式要求每个启用节点都有有效 ProxyID，且对应代理自身有可用的 fallback 策略；否则节点故障会直接影响生产调用。`GROK_ALLOW_MISSING_SUB2API_PROXY_IDS=true` 只保留给低层迁移检查，正式 `register_and_import.py` 仍会在注册前拒绝缺少 ProxyID 的粘性配置。

健康预检会把本轮不健康节点排除出 lease 池，只要至少一个健康节点存在即可继续。节点若在注册中途断开，不盲目重放同一 attempt，因为上游账号可能已经创建；保留失败现场，下一批由持久游标使用下一个健康节点。

运行中立健康检查：

```bash
python3 scripts/check_proxy_pool.py \
  --private-dir /root/grok-build-auth/private \
  --attempts 3 \
  --timeout 10
```

独立运行 `check_proxy_pool.py` 时，任一节点成功率低于 2/3、TLS 校验失败、出口漂移或不同 ref 实际同出口会整体失败，适合完整池审计。批次编排器会排除不健康或重复出口节点，只要至少一个健康节点仍可继续；manifest 会记录每个节点的脱敏原因。不要把论坛节点凭据、完整代理 URL 或出口 IP 写进仓库和公开日志。

### 4.4 主 V2Ray 与 Grok 代理池隔离

服务器上的公共代理和 Grok 注册代理池是两个独立服务：

| 服务 | 配置 | 监听范围 | 用途 |
|---|---|---|---|
| `v2ray.service` | 从 `systemctl cat v2ray.service` 发现 | 以主配置实际 inbound 为准 | 其他客户端和服务器日常代理 |
| `v2ray-grok-pool.service` | 从 `systemctl cat v2ray-grok-pool.service` 发现 | 以专用配置实际 loopback inbound 为准 | grok-build-auth 注册、OAuth 和 preprobe 代理池 |

严禁在 `v2ray.service.d/*.conf` 中把主服务 `ExecStart` 覆盖为 Grok 专用代理池配置。这会替换主服务的全部 inbound，其他客户端会立即断线。

只读检查：

```bash
python3 scripts/check_v2ray_isolation.py
```

正确结果必须同时满足：

- `v2ray.service` 的 `ExecStart` 指向 `/etc/v2ray/config.json`。
- `v2ray-grok-pool.service` 指向 `/etc/v2ray/grok_pool.json`。
- 主服务配置声明的全部既有 inbound 仍存在。
- 代理池配置声明的全部 inbound 只能绑定 loopback；禁止 Docker 网关、`0.0.0.0` 或公网监听。
- 每个本机 inbound 必须按端口严格路由到对应 `proxy-01` 至 `proxy-08`。
- 注册专用模式下 Sub2API 账号 `proxy_id` 应为空，节点故障不会影响生产调用。
- 两份配置均通过 `v2ray test`。

如发现主服务 drop-in 指向 `grok_pool.json`，先保存现场并确认配置有效，再删除该精确 drop-in、执行 `systemctl daemon-reload`，分别重启两个服务。不要删除 `/etc/v2ray/config.json`，也不要把代理池合并进公共服务。

### 4.5 运行服务器流程

加 `--confirm-production-write` 前，操作者必须明确确认：当前目录是 `/root/grok-build-auth`；`SUB2API_URL`、PostgreSQL 容器和数据库是目标部署；目标分组经配置和 Sub2API 元数据核对为独立 `grok`，不得假定固定组 ID；本次账号数和 worker 数正确；已授权创建邮箱、注册上游账号、写入 Sub2API 和建立数据库恢复点。任一项不明确时先停在预检，不执行生产写入。

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

### 4.6 Web 控制台

```bash
set -a
. private/console.env
set +a
bash start_web_console.sh --host "$GROK_CONSOLE_HOST" --port "$GROK_CONSOLE_PORT"
```

控制台必须只监听 loopback。若通过 Nginx 暴露，必须使用 Basic Auth 或等效强认证，并保留长请求超时。不要匿名公开生产操作接口。

控制台支持环境检查、创建批次、实时日志、历史批次和导入续跑；当前不提供 OAuth recovery 和 `--no-import` UI，这两项使用 CLI。

### 4.7 真实执行顺序

1. 获取跨进程批次锁。
2. 校验 `runtime.env` 权限、必填字段、worker、目标分组和 base URL。
3. 如启用代理池，校验 TLS、成功率、出口稳定性和重复出口；只有显式启用导入后粘性时才校验 ProxyID。
4. 创建唯一 Mailu 邮箱。
5. 每个 attempt 独立运行 `run.py`，完成注册、SSO、OAuth 和 auth JSON。
6. 校验结果邮箱、auth 路径边界、凭据完整性并聚合 bundle。
7. 对每个 auth 直接请求 Grok CLI `/responses` 做 preprobe。
8. 403、传输错误和 5xx 可按配置等待重试；429 不自动重试。
9. 按 access token hash 查询已存在账号，只导入缺失项；写入前创建数据库恢复点。
10. 对本批精确账号统一收口 Grok 分组、官方 CLI base URL、凭据和调度状态；注册专用模式显式清空旧 ProxyID，粘性模式才写入 ProxyID。
11. 验证数据库精确状态。
12. 对每个导入账号 ID 调用 Sub2API 指定账号 SSE test，验证 Sub2API 正常生产路径和 OAuth；默认不依赖注册代理。
13. 使用 Grok 分组 API Key 调用 Sub2API `/v1/responses` 做 postprobe。
14. 全部成功后 manifest 进入 `imported-preprobed`。

新账号资格可能需要传播。preprobe 首次 403、等待后变 200，不代表 token 只有几分钟有效；默认最多等待 900 秒，每 60 秒重试。

### 4.8 成功判定

不要只看进程退出码。检查：

```bash
jq '{status, imported_ids, preimport_auth_probes, exact_account_state, postimport_account_probes, postimport_group_probe}' \
  private/runs/<batch-id>/manifest.json
```

必须满足：

- `status=imported-preprobed`
- 预探针通过数量等于实际导入 auth 数量
- `imported_ids` 数量匹配
- `exact_account_state` 通过平台、类型、状态、分组、base URL、凭据和代理检查
- `postimport_account_probes.passed=true`；正常账号出现 SSE `test_complete success=true`，明确
  402/429、`free-usage-exhausted` 或 rolling 24-hour quota 的账号记录为
  `availability=usable_exhausted`、`code=RATE_LIMITED`，仍视为可用并保留
- `postimport_group_probe.status=200`
- postprobe 为 completed 且输出匹配

### 4.9 失败恢复

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

### 4.10 批次安全收口

账号完成导入、逐账号测试和分组测试后，先对精确批次执行只读 dry-run：

```bash
python3 scripts/finalize_grok_batch.py --batch <batch-id>
```

dry-run 会重新核对本地 auth、bundle、生产账号的 email/subject、access/refresh 凭据、唯一 Grok 分组、调度状态、官方 CLI base URL、实时指定账号测试、分组测试和所有数据库恢复点。正常账号与明确 free-usage/spending-limit 的 402/429 账号均可收口；容量、网络、permission、revoked 和未知错误必须停止。

全部通过后再提交精确清理：

```bash
python3 scripts/finalize_grok_batch.py \
  --batch <batch-id> \
  --confirm-cleanup
```

提交会先原子写入脱敏 handoff 和带 source hash 到账号 ID 映射的 checkpoint，再删除已成功交接账号的精确 auth、token bundle 和成功注册日志。密码恢复结果、失败尝试材料和本批数据库恢复点默认保留；最终 `manifest.json`、`handoff.json` 和 `import/*.json` 不包含邮箱、token 或密码。

## 5. 外部客户端路径

### 5.1 部署边界和要求

外部客户端代码位于本仓库 `clients/windows/`。要求：

- Python 3.12 或 3.13。
- full 模式由客户端启动受控浏览器；export-only 模式附着用户明确启动的 Microsoft Edge CDP。
- 客户端本机有真实可用的 HTTP/SOCKS 代理。
- 能通过 HTTPS 访问 bridge 公网入口。

安装：

```bash
cd /root/grok-build-auth/clients/windows
python -m pip install -r requirements.txt
copy config.example.json config.json
```

先用 1 路并发完成全链验收，再考虑提高到 2。旧的 5 到 15 路建议不适用于当前长探针和不稳定免费出口。

### 5.2 `config.json`

最小生产配置：

```json
{
  "email_provider": "cloudflare",
  "cloudflare_api_base": "https://<bridge-domain>",
  "bridge_health_path": "/health",
  "cloudflare_path_accounts": "/admin/new_address",
  "cloudflare_path_domains": "/api/domains",
  "cloudflare_path_token": "/api/token",
  "cloudflare_path_messages": "/api/mails",
  "cloudflare_auth_mode": "bearer",
  "cloudflare_api_key": "<bridge-management-secret>",
  "defaultDomains": "<mail-domain>",

  "proxy": "<client-local-proxy-url>",
  "register_count": 1,
  "max_concurrency": 1,
  "hide_window": true,
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
  "cpa_require_probe_passed": true,
  "cpa_push_timeout_sec": 240,

  "mint_proxy": "",
  "mint_timeout_sec": 300,
  "mint_required": true
}
```

注意：

- 面向业务统一称为 Sub2API auth。`cpa_*`、`cpa_auths/` 和 `cpa_export` 是现有客户端的兼容键、目录和模块名；不要仅为改名破坏已有配置或脚本。

- `proxy` 必须是客户端本机实际监听地址，不能照抄服务器的 `127.0.0.1:<port>`。
- bridge 健康检查不是 `/health` 时，通过 `bridge_health_path` 填写实际公网路径，例如 `/bridge-health`。
- `mint_proxy` 为空时复用注册代理。
- `hide_window=true` 保持 headed Chromium，但在 Windows 使用 `SW_HIDE` 隐藏任务窗口，不占用任务栏或抢占前台；不要改成 headless 绕过真实页面流程。
- 邮箱 API 和 Sub2API auth push 当前可使用同一 bridge 管理凭据，但应长期拆分权限。
- `config.json` 含管理密钥，权限必须为 `0600`，不得打包分享或提交 Git。
- `mint_timeout_sec` 应覆盖 device flow 和协议 fallback；60 秒在网络波动时偏紧。

### 5.3 运行

Windows：

```powershell
cd D:\path\to\grok-build-auth\clients\windows
python grok_register_ttk.py
```

按提示输入并发数 `1`。

Linux/Xvfb 示例：

```bash
cd /root/grok-build-auth/clients/windows
DISPLAY=:99 bash -c 'echo 1 | .venv/bin/python3 grok_register_ttk.py'
```

部署服务器模拟客户端使用正式编排入口，不恢复历史 `/tmp` runner：

```bash
cd /root/grok-build-auth
DISPLAY=:99 clients/windows/.venv/bin/python3 scripts/run_linux_client_full.py \
  --target 1 \
  --routes 1 \
  --attempts-per-route 20 \
  --proxy-ref <healthy-ref>
```

canary 必须同时出现本地 auth、bridge `action=created`、`probe=passed` 和精确账号 ID。两路后台批量使用两个独立代理 ref、两个 route 目录、每路单浏览器；不能用数据库全池增长代替本批归因。

脚本没有稳定的非交互参数接口，不要把位置参数当 CLI 选项使用。

运行前从仓库根目录执行预检：

```powershell
python scripts\windows_client_preflight.py `
  --config clients\windows\config.json `
  --skip-cdp
```

`--skip-cdp` 只适用于 full 模式。export-only 模式先用远程调试端口启动 Edge：

```powershell
& "${Env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222
netstat -ano | findstr "LISTENING" | findstr "9222"
python scripts\windows_client_preflight.py --config clients\windows\config.json
```

已经在 Edge 登录目标账号时，可跳过注册直接铸造、推送和验证。密码通过环境变量或隐藏提示提供，不写进命令行：

```powershell
$Env:GROK_ACCOUNT_PASSWORD = Read-Host -AsSecureString | ConvertFrom-SecureString -AsPlainText
python scripts\windows_export_logged_in.py `
  --config clients\windows\config.json `
  --email <account-email> `
  --require-created
Remove-Item Env:GROK_ACCOUNT_PASSWORD
```

已有 auth 文件时：

```powershell
python scripts\windows_push_auth.py `
  --config clients\windows\config.json `
  --auth <xai-auth.json> `
  --require-created
```

需要同时验证公网业务入口时，设置 `GROK_GROUP_API_KEY` 环境变量，并追加 `--responses-base https://<sub2api-domain>`。

Windows UI 排障要点：

- OneTrust/Cookie 弹层可能包含零尺寸隐藏按钮，只操作有尺寸的可见元素。
- 邮箱提交按钮无响应时，优先在邮箱框按 Enter，再回退 `form.requestSubmit()`。
- 验证码优先从邮件 subject 的 `XXX-XXX` 提取，填表时移除连字符。
- 资料页字段已填、本地账号文本已写都可能是假成功；至少要进入 `grok.com` 并出现 `sso` 或 `sso-rw`，然后继续 OAuth 和 push。
- 禁止使用硬编码账号密码的 `export_one.py`、`one_shot_pipeline.py` 或历史 patch/debug 脚本。

### 5.4 客户端内部流程

1. 通过 bridge 创建 Mailu 邮箱并取得 1 小时邮箱 JWT。
2. 浏览器通过客户端代理完成 x.ai 注册和邮箱验证码。
3. 取得 SSO；此时尚不写正式账号记录或 Sub2API auth。
4. 强制确认浏览器离开 TOS gate，并通过同源 `/rest/auth/set-birth-date` HTTP 200。
5. 在真实网页聊天框提交随机 canary，只接受 assistant 角色中的精确回复；网页 403 立即失败。
6. 浏览器门禁通过后保存本地恢复记录并尝试 device OAuth；`curl_cffi` 明确发生 TLS 握手错误时，保持同一代理和证书校验改用标准 `requests`；超时、接收失败等可能已到达服务端的请求不重放；失败时再复用 SSO 走协议 OAuth。
7. 要求同时得到 access token 和 refresh token。
8. 客户端 preprobe 通过后写出 `cpa_auths/xai-<email>.json`；目录名是兼容名称。首次 `PERMISSION_DENIED/403` 立即进入 pending，不做延迟复测；网络、无效响应和普通上游错误仍可按配置短重试。
9. 将同一 Sub2API auth JSON push 到 bridge。
10. bridge 在写库前执行上游 preprobe，通过后才 create/update；导入后再执行指定账号 test，失败时按新建/更新路径回滚或恢复。

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

Sub2API auth push（CLIProxyAPI-compatible schema）：

```http
POST /v0/management/auth-files?name=xai-<email>.json
X-Management-Key: <bridge-management-secret>
Content-Type: application/json

<Sub2API xAI OAuth auth JSON>
```

请求体最大 1 MiB。状态含义：

| HTTP | 含义 |
|---|---|
| 200 | 返回 `status=ok`、`account_id`、`probe=passed`，账号已通过指定账号探针并入组 |
| 400 | email/token 结构不完整、token 临近过期或缺 `sub` |
| 401 | 邮箱 API Bearer 错误或邮箱 JWT 失效 |
| 403 | bridge 管理密钥错误 |
| 413 | 请求体超限 |
| 422 | 探针未通过或 `STALE_AUTH`；结合 `error_code`、`imported`、`action` 和账号 ID 判断是否发生过导入后回滚，不可只凭状态码推断零写入 |
| 500 | Sub2API 创建、更新、查询或服务端依赖失败 |

### 5.6 Bridge 的探针、导入和入组

当前 bridge 按邮箱生成稳定账号名。收到 auth 后：

1. 校验 email、access token、refresh token、JWT `exp` 和 `sub`。
2. 在数据库写入前直连 Grok CLI `/responses` 做 preprobe；未通过返回结构化 422，不创建或更新账号。
3. 查询同身份现有账号；候选 refresh token 与数据库不同且 access JWT 明显更旧时返回 `STALE_AUTH`，零写入，防止历史 AUTH 覆盖已轮换凭据。
4. preprobe 通过后，按稳定账号身份 create/update 为无组、不可调度候选；同时把 access JWT `exp` 规范化为 Sub2API 使用的 `expires_at`。
5. 调用 Sub2API 指定账号 Responses test 做 postimport 验证。测试可能触发 token refresh 和 refresh token 轮换。
6. 测试通过后只更新分组和调度，不再提交测试前的 credentials；提升前后必须核对 access/refresh token hash 和 `_token_version` 未回退。
7. postimport 失败时，新建账号必须按精确 ID 回滚；更新账号恢复旧元数据时必须保留测试期间已经产生的更新凭据。回滚失败必须显式报错，不能声称零写入。
8. 成功返回 `probe=passed`、`imported=true`、`action` 和 `account_id`。

同邮箱重推会更新同一账号，适合 OAuth token 更新和确认可恢复错误后的幂等重推。任何 422 都必须结合响应字段和数据库精确状态核对。

### 5.7 外部客户端成功判定

仓库内客户端已修复历史口径缺陷：`register_one()` 会检查 OAuth、auth 写盘、push 和 `probe=passed`，失败不会增加成功计数；退出时也不再扫描并终止所有调试 Chrome 或 Google 更新进程。旧的外部副本仍可能保留这些缺陷，必须以本仓库版本为准。

必须同时确认：

- 本地 Sub2API auth JSON 已生成且权限安全。
- 日志明确显示 push HTTP 200。
- bridge 响应含 `account_id` 和 `probe=passed`。
- Sub2API 账号为 active、schedulable、唯一 Grok 分组、官方 CLI base URL。
- Grok 分组 `/v1/responses` 返回 200。

### 5.8 外部客户端失败恢复

- 邮箱 API 401：检查 `cloudflare_auth_mode=bearer` 和管理凭据。
- 验证码失败：客户端最多更换邮箱重试 3 次。
- OAuth 失败：保留邮箱、密码和 SSO，从已有账号补 OAuth，不要重新建号。
- push 422：读取 `error_code`、`imported`、`action` 和账号 ID，再核对数据库。`STALE_AUTH` 表示本地 AUTH 已落后于 Sub2API，禁止重推；资格传播或暂时上游故障只允许从明确的失败 checkpoint 重试当前凭据。`invalid_grant`、`Refresh token has been revoked` 或 `GROK_OAUTH_TOKEN_REFRESH_FAILED` 必须先重新登录铸造 OAuth；429/402 额度用尽仍算可恢复可用状态。不能无分类地重复注册或删除账号。
- push 超时：先查 bridge 日志和 Sub2API 账号，不能假定服务端没有写入；确认后再幂等重推。
- push 403：核对 management secret，不要把密钥放到命令行历史。
- push 500：查看 bridge journal、Sub2API 和 PostgreSQL；不要直接重复注册新账号。

### 5.9 AUTH 快照和 refresh 所有权

- `access_token` 实测约 6 小时；`refresh_token` 不会因为账号闲置 6 小时或几天未调用而按此周期自然过期。
- xAI 刷新响应可能轮换 `refresh_token`。Sub2API 成功接管账号后，由其后台刷新器和请求内刷新共同维护数据库中的当前 token；不要再建立独立客户端 cron 与 Sub2API 争用同一轮换链。
- `cpa_auths/xai-*.json` 是注册、恢复和首次交接的本地快照，不会从 Sub2API 反向同步。交接成功后可按敏感审计材料保留，但不能作为持续刷新权威，也不能在 checkpoint 缺失时批量回灌。
- `cpa_reprobe.py --include-verified` 只重试 checkpoint 明确记录为 push 失败、且完整凭据 fingerprint 仍一致的文件。没有精确失败 checkpoint 的 verified AUTH 会跳过，不探测、不刷新、不推送。
- 客户端若确实完成 refresh，会先原子写回当前 AUTH 文件，再进行二次 probe 和 push；verified AUTH 明确 revoked 时保留文件和记录，不自动删除。
- revoked token 无法靠定时刷新恢复。长期应保留账号密码、可收信邮箱和 SSO 供 remint；恢复时重新登录铸造 OAuth，并更新原 Sub2API 账号，不创建重复账号。SSO 的服务端寿命未知，不能把它视为永久凭据。

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

Sub2API 已原生处理 Grok CLI OAuth Responses 请求：

- OAuth 账号默认并只需使用官方 `https://cli-chat-proxy.grok.com/v1`。
- 出站请求由当前调度账号生成 `Authorization: Bearer <access_token>`。
- 固定补齐 `grok-cli/0.2.93` User-Agent、`X-XAI-Token-Auth`、客户端版本和客户端标识。
- 对 `grok-4.5*` 历史 `input` 中 `type=reasoning` 且 `content=null` 的项目只删除该空字段。
- 流式和非流式响应共用同一原生请求构造，不依赖独立 sidecar。

生产不应再设置 `XAI_ALLOW_UNSAFE_URL_OVERRIDES`，也不应保留 `grok-cli-proxy` 容器。官方 CLI host 已在 Sub2API 的 xAI allowlist 中，恢复默认 URL 校验可避免内部 HTTP URL 和任意上游覆盖扩大 SSRF 范围。

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

Sub2API 启动后会立即检查，并按配置周期（当前默认 5 分钟）扫描 active OAuth 账号；Grok 在距离过期至少 1 小时时进入后台刷新窗口，请求路径也会按需刷新。该机制已经覆盖 `grok`，无需额外定时任务。若看到“原 access token 约 6 小时后突然 revoked”，应优先检查数据库 refresh token 是否被旧 AUTH 覆盖，而不是把 6 小时误判为 refresh token 生命周期。

错误判读：

| 状态 | 优先判断 |
|---|---|
| 401 | access token 过期、refresh 失败、scope 错误 |
| 402 | 不能统一解释为欠费；先检查 CLI headers、base URL、通道兼容和明确额度证据 |
| 403 | entitlement、资格传播、订阅或风控 |
| 429 | 限流/额度；看 `Retry-After`、reset 和响应体，不要立即永久禁用账号 |
| 502 | token refresh、上游连接或内部转发失败；查 ops 日志中的真实 reason 和账号 ID |

## 8. 发布、验证和回滚

推荐发布顺序：

1. 备份账号、分组、API Key 和相关数据库表。
2. 创建独立 Grok 分组和专用 Key。
3. 部署包含原生 Grok CLI headers 和 reasoning sanitizer 的 Sub2API 镜像。
4. 确认 `XAI_ALLOW_UNSAFE_URL_OVERRIDES` 未设置，且没有独立 Grok sidecar。
5. 先导入 1 个账号，保持隔离直至指定账号探针通过。
6. 校验数据库和 Redis 中账号 base URL 都是官方 CLI URL。
7. 执行公网非流式和流式 Responses。
8. 再逐步扩大账号数和代理节点数。

回滚前必须确认备份、目标账号和允许的中断窗口。应用版本回滚与账号 URL 回滚必须配套：旧版若不原生补齐 CLI headers，就不能让账号继续直连官方 CLI URL；此时应先停止 Grok 专用 Key，再恢复经过验证的旧完整部署。不要只恢复 sidecar 或只改数据库而忽略 Redis/outbox。

数据库恢复、删除账号、停止服务、删除镜像或清理邮箱都属于高风险操作。执行前创建数据库恢复点，导出候选账号 ID 清单并逐 ID 核对证据；只能处理用户已授权且由本批 manifest、auth 到账号映射或数据库结果精确证明归属的目标。

## 9. 安全要求和当前安全债

永远不要提交或公开：

- `.env`、`private/`、`config.json` 中的真实秘密
- access/refresh/id token、SSO、邮箱 JWT、账号密码
- Mailu、Sub2API、bridge、Basic Auth 和代理凭据
- auth JSON、bundle、result、日志和数据库备份

仓库内 bridge 已改为从环境变量或 credential file 读取 Mailu、Sub2API 和 bridge 密钥，并使用结构化 JSON 判断指定账号 probe。部署时从 `private.example/bridge.env.example` 创建 `private/bridge.env`。

仍有以下安全债：

- 历史部署曾把 Mailu API token 和 Sub2API admin key 硬编码在源码中。迁移到仓库版本后仍必须轮换旧密钥。
- access JWT 只解码 payload，未验证 xAI 签名、issuer 和 audience；真实 Sub2API probe 是最终门禁，不能移除。
- 幂等键是 email/账号名，不是 token hash 或 `sub`。
- 邮箱 JWT 内含 IMAP 密码，虽然签名且一小时过期，但未加密。
- CORS 为 `*`，bridge 使用标准库 WSGI server；公网入口必须由 Nginx 鉴权、限流和请求大小/超时保护。
- Web 控制台依赖反向代理认证，应用自身没有独立 CSRF/Origin 门禁。

这些债务不否定当前真实成功链路，但不能宣称 bridge 已完全硬化。

秘密一旦进入 Git 历史，先轮换/撤销，再清理历史并使用 `force-with-lease`；只删除工作区文件不够。

## 10. 日常检查清单

运行前：

- `runtime.env`、代理池和外部客户端 `config.json` 权限为 `0600`。
- Mailu、PostgreSQL、Sub2API 和 bridge 健康，旧 Grok sidecar 不存在。
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
