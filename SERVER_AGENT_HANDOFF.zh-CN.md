# grok-build-auth 服务器 Agent 交接手册

## 1. 目标与审计基线

- 上游：`https://github.com/dongguatanglinux/grok-build-auth`
- 已审计提交：`9f94e5b4c78272b06bd1899d648854db279924d2`
- 项目性质：一次性 Python CLI，不是 Web 服务，也不是 CLIProxyAPI 本体。
- 作用：研究 x.ai 注册、SSO、OAuth PKCE 流程，并生成 CLIProxyAPI 可读取的 xAI OAuth JSON。

只允许用于用户自己的账号、明确授权的研究环境。不要批量注册、转售账号、绕过平台规则，或对未经授权的目标运行。

## 2. 是否需要派生仓库

结论分两种情况：

1. 只做临时源码阅读或一次性隔离测试：不必 fork，直接 clone 并固定到上述 commit。
2. 要长期放到服务器使用：建议建立用户自己的加固仓库，因为部署前有必须修改的安全问题。

注意：GitHub 的公开仓库 fork 通常仍是公开仓库。若补丁、部署配置或历史不希望公开，应创建一个新的私有仓库并导入上游，而不是依赖公开 fork。任何密钥和运行产物都不得提交。

推荐分支模型：

```text
upstream/main        仅跟踪原作者
main                 用户已审计的稳定版本
hardening/server     服务器安全补丁
```

同步上游时先审查差异，不要在服务器自动 `git pull`。

## 3. 部署前必须修改

服务器 Agent 必须先完成并验证以下补丁，之后才能运行真实认证链路。

### 3.1 凭据只保留一份并设置严格权限

- 禁止保存明文账号密码和 SSO JWT。
- 默认不生成 `accounts_output/`、`sso_output/` 和包含完整 token 的重复 `oauth_output/`。
- 只保留 CLIProxyAPI 确实需要的 auth JSON。
- 敏感目录权限 `0700`，文件权限 `0600`。
- 使用同目录临时文件、`fsync`、`os.replace` 的原子写入方式。
- 进程设置 `umask 077`；systemd 使用 `UMask=0077`。

涉及位置：`run.py`、`xconsole_client/sso.py`、`xconsole_client/xai_oauth.py`。

### 3.2 日志完全脱敏

- 不输出邮箱验证码。
- 不输出 access token、refresh token、SSO、cookie、OAuth code 的任何前缀。
- 邮箱默认脱敏。
- URL 日志必须去掉 query 和 fragment。
- 异常消息写日志前统一过滤敏感字段。
- 服务器上默认禁止 `--oauth-debug`。

### 3.3 限制 token 的外发地址

- 删除或默认禁用 `xai_build_quota_probe.py --use-auth-base-url`。
- 若保留，只允许 `https://cli-chat-proxy.grok.com`，拒绝 userinfo、非 HTTPS、IP、重定向到其他主机。
- `--cliproxyapi-base-url` 使用相同 allowlist，避免生成把 token 发往任意地址的 auth 文件。

### 3.4 OAuth 回调只允许 loopback

- `--host` 只允许 `127.0.0.1` 或 `::1`。
- 不开放 OAuth callback 公网防火墙端口。
- 保留 PKCE 和 state 校验。

### 3.5 限制动态跳转和 Cookie 范围

- `success_url`、`return_to` 使用精确主机 allowlist。
- 不把任意 session cookie 同时扩展到 `.x.ai` 和 `accounts.x.ai`。
- 仅允许预期 cookie 名称，并保持来源域范围。

### 3.6 锁定依赖

- 在干净的 Linux Python 环境中生成完整 lock 文件。
- 固定直接和传递依赖版本；条件允许时使用 hash 校验。
- 部署固定 Git commit，不使用浮动 `main`。

### 3.7 代理链修复

上游当前没有让代理完整覆盖注册和 OAuth 会话。若服务器必须使用代理，需要修复并测试：

- `run.py` 创建注册 client 时传入代理。
- `FingerprintTransport` 创建 session 时实际使用代理。
- OAuth 复用注册 session 时不能覆盖掉带代理的 session 设置。
- 代理地址可能含凭据，禁止写入日志。

## 4. 建议但非强制的修改

- 增加 `--no-playwright-fallback`，使协议失败时可以明确停止，而不是隐式尝试浏览器。
- 将所有运行数据目录通过参数或环境变量移到 `/var/lib/grok-build-auth`。
- 若仍保留自动注册，密码至少使用 128 bit 以上随机量；默认强制 `count=1`、`threads=1`。
- 移除 Cloudflare 邮箱模块对 `CLOUDFLARE_MCP_READ_ALL_TOKEN` 的回退，只接受专用最小权限 token。
- 添加单元测试：安全写文件、日志脱敏、URL allowlist、loopback 校验、代理传递。
- 添加 CI：静态编译、测试、依赖审计、secret scan。

## 5. 服务器要求与下载项

推荐环境：Ubuntu 22.04/24.04 x86_64，Python 3.11 或 3.12。

基础系统包：

```bash
sudo apt-get update
sudo apt-get install -y git ca-certificates curl python3 python3-venv python3-pip
```

Python 基础依赖来自 `requirements.txt`：

- `curl_cffi`
- `requests`
- `python-dotenv`

Playwright 是可选回退。只有决定保留无界面浏览器回退时才安装：

```bash
python -m pip install playwright
python -m playwright install --with-deps chromium
```

不要安装 Nginx、Caddy、PM2 或 Supervisor。本项目没有对外 Web 服务。若另行部署 CLIProxyAPI，应独立评估其监听地址、鉴权、TLS 和管理接口。

## 6. 外部配置

### 推荐路径：自有既有账号 OAuth

优先使用已有、归用户所有的账号完成官方可见 OAuth，不运行自动注册、临时邮箱和验证码求解链路。该路径不需要：

- `YESCAPTCHA_API_KEY`
- `TEMPMAIL_API_KEY`
- Cloudflare D1 邮箱配置

它可能需要浏览器完成授权；在无桌面的服务器上，应由服务器 Agent 设计 loopback/SSH 隧道流程，并在真正授权前向用户确认。

### 上游完整注册路径（高风险，不作为默认执行方案）

只有用户明确确认授权范围和平台规则后才可考虑。需要：

```dotenv
YESCAPTCHA_API_KEY=<secret>
TEMPMAIL_API_KEY=<secret>
CLIPROXYAPI_AUTH_DIR=/var/lib/grok-build-auth/cliproxyapi-auth
# HTTPS_PROXY=http://host:port
# HTTP_PROXY=http://host:port
```

Cloudflare 自建邮箱模式改为：

```dotenv
YESCAPTCHA_API_KEY=<secret>
CLOUDFLARE_API_TOKEN=<dedicated-minimum-scope-token>
CLOUDFLARE_ACCOUNT_ID=<id>
CLOUDFLARE_D1_DB_ID=<id>
ALIAS_MAIL_DOMAINS=mail.example.com
CLIPROXYAPI_AUTH_DIR=/var/lib/grok-build-auth/cliproxyapi-auth
```

Cloudflare 模式不是只填变量即可。用户必须已经部署收信 Worker、Email Routing、D1 数据库及项目期待的 `address`、`raw_mails` 表结构。当前仓库没有提供这些基础设施。

## 7. 网络与端口

默认只需要出站 HTTPS 443。按代码可能访问：

- `console.x.ai`
- `accounts.x.ai`
- `auth.x.ai`
- `grok.com`
- `auth.grokusercontent.com`
- `cli-chat-proxy.grok.com`
- `api.yescaptcha.com`（仅完整注册路径）
- `api.tempmail.lol`（仅 Tempmail）
- `api.cloudflare.com`（仅 Cloudflare 邮箱）

不需要开放公网入站端口。OAuth callback 必须保持 loopback。

## 8. 推荐目录和权限

```bash
sudo useradd --system --create-home --home-dir /opt/grok-build-auth \
  --shell /usr/sbin/nologin grokbuild 2>/dev/null || true

sudo install -d -o grokbuild -g grokbuild -m 0700 \
  /opt/grok-build-auth/app \
  /opt/grok-build-auth/venv \
  /var/lib/grok-build-auth \
  /var/lib/grok-build-auth/cliproxyapi-auth

sudo install -o root -g grokbuild -m 0640 /dev/null /etc/grok-build-auth.env
```

不要让普通备份、CI artifact、journald 或 Web 管理面板收集 auth 目录。

## 9. 安装流程

以下命令应将仓库 URL替换为用户自己的加固仓库。若尚未创建，服务器 Agent先在本地完成补丁和测试，不得直接运行真实链路。

```bash
sudo -u grokbuild git clone <USER_HARDENED_REPOSITORY_URL> /opt/grok-build-auth/app
sudo -u grokbuild git -C /opt/grok-build-auth/app checkout <AUDITED_COMMIT>

sudo -u grokbuild python3 -m venv /opt/grok-build-auth/venv
sudo -u grokbuild /opt/grok-build-auth/venv/bin/python -m pip install --upgrade pip
sudo -u grokbuild /opt/grok-build-auth/venv/bin/pip install \
  -r /opt/grok-build-auth/app/requirements-lock.txt
```

安装后先执行无外部副作用的检查：

```bash
sudo -u grokbuild /opt/grok-build-auth/venv/bin/python \
  /opt/grok-build-auth/app/run.py --help

sudo -u grokbuild /opt/grok-build-auth/venv/bin/python -m compileall -q \
  /opt/grok-build-auth/app

sudo -u grokbuild git -C /opt/grok-build-auth/app status --short
```

## 10. 首次真实运行门槛

服务器 Agent 在执行真实认证、账号创建、验证码求解或模型额度探测前，必须确认：

- 用户明确授权该账号和目标环境。
- 已应用本手册全部“必须修改”项。
- 使用单账号、单线程；不得自动定时批量执行。
- 敏感目录和文件权限测试通过。
- 日志测试确认不包含验证码、密码、token、cookie、SSO 或 OAuth code。
- 出站目标受 allowlist 控制。
- 已说明账号封禁、额度消耗和第三方服务费用风险。

`xai_build_quota_probe.py` 会发送真实模型请求，并非纯本地检查。除非用户明确允许消耗少量额度，否则不要运行。

## 11. CLIProxyAPI 集成

本项目只生成 auth JSON，不会安装 CLIProxyAPI。

- 同机部署：让 CLIProxyAPI 的 `auth-dir` 指向 `/var/lib/grok-build-auth/cliproxyapi-auth`。
- 容器部署：把该目录作为独立 volume 挂载。先确认 CLIProxyAPI 是否会原地刷新 token，再决定只读或读写挂载。
- 跨服务器：通过受控秘密分发同步，不能使用 Git、公开对象存储或未加密传输。
- 默认上游应保持 `https://cli-chat-proxy.grok.com/v1`，不要擅自改成 `api.x.ai/v1`。

## 12. 验收标准

- 代码固定到已审计 commit。
- 测试和静态编译通过。
- `git status --short` 只包含明确归属的部署补丁。
- 无秘密进入 Git 历史。
- 敏感目录 `0700`，文件 `0600`。
- 默认无公网监听端口。
- 日志中无秘密或秘密前缀。
- auth JSON 的 `base_url` 和实际请求目标均通过精确 allowlist。
- 只生成完成目标所需的最少凭据文件。
- 提供回滚 commit 和上游差异清单。

## 13. 许可证与分发

保留上游 `LICENSE` 和 `NOTICE`。仓库声明 MIT，但 NOTICE 还包含用途限制，两者的法律效果存在表述冲突。公开分发、商业化或向第三方提供服务前，应由法务判断，不要宣传为“无附加条件”。
