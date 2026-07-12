# Grok Sub2API Runbook

## 目录

- 服务器 canary
- Windows 客户端
- OAuth 恢复
- 账号状态
- 代理池
- Bridge 和验收
- 清理边界

## 服务器 canary

1. 核对 `private/runtime.env` 和 `private/proxies.json` 权限为 `0600`。
2. 确认 Mailu、Sub2API、PostgreSQL、bridge 和 Grok 分组身份。
3. 运行 `scripts/check_v2ray_isolation.py` 和 `scripts/check_proxy_pool.py`。
4. 固定 `--count 1 --workers 1 --failure-policy abort`。
5. `protocol-yescaptcha` 不点击浏览器：邮件验证码通过 IMAP 读取，Turnstile 调用配置的 YesCaptcha；premium M1 按当前源码预计每次约 30 points。
6. 检查 manifest 的 preprobe、精确账号状态、指定账号 SSE postprobe 和分组 postprobe。

## Windows 客户端

1. 使用 Python 3.12/3.13，从 `config.example.json` 创建受限配置。
2. full 模式使用客户端本机真实代理并执行 `--skip-cdp` 预检；首次并发输入 1。
3. export-only 才需要用户明确启动 Edge CDP 9222；只附着，不结束用户 Edge。
4. OneTrust 可能有零尺寸按钮；邮箱提交无效时先 Enter，再 `requestSubmit()`。
5. 验证码优先从邮件 subject 的 `XXX-XXX` 解析，填表时去掉连字符。
6. 资料字段写入或账号文本落盘都不是成功；必须继续 OAuth、auth 写盘、bridge push 和 probe。
7. device OAuth 超时可回退 SSO 协议 OAuth；不要重新注册同一账号。

## OAuth 恢复

出现以下错误时标记 revoked：

```text
invalid_grant
Refresh token has been revoked
GROK_OAUTH_TOKEN_REFRESH_FAILED
```

恢复顺序：

1. 查受限账号产物是否保存邮箱、密码和 SSO，不打印值。
2. 有密码/SSO时重新铸造 OAuth，先直接请求官方 CLI `/responses` 验证新 auth。
3. 更新原 Sub2API 账号凭据，不创建重复账号；执行指定账号 probe 后再恢复调度。
4. 无密码但邮箱可收信时走 xAI 忘记密码；邮箱也不存在时无法安全恢复。
5. 旧 refresh token、数据库备份或旧 auth 文件不能替代重新登录。

## 账号状态

| 结果 | 含义 | 动作 |
|---|---|---|
| 指定账号 test 完成 | 当前真实可用 | 保持入组和调度 |
| 429 rolling 24h | 免费额度耗尽 | 临时冷却，等待 reset |
| refresh revoked | token 不可刷新 | 重新登录铸造 OAuth |
| 403 entitlement | 资格传播、订阅或风控 | 等待并复测，不立即删除 |
| bridge 422 | probe 未通过且已隔离 | 按内部错误分类后恢复 |

数据库 `active`、`schedulable` 或 refresh token 字段存在都不是可用证据。审计时调用指定账号测试，不用分组请求代替。

## 代理池

- 默认只覆盖注册、OAuth 和 preprobe；Sub2API 生产调用不绑定注册节点。
- 启动前检查成功率、TLS、出口稳定和重复出口，只租用健康节点。
- 持久游标使连续单账号批次轮换节点。
- 节点在 attempt 中途失败时不盲目重放；账号可能已创建，先检查结果和邮箱。
- 续跑时原注册节点若已不健康，preprobe 选择当前健康节点作为 fallback，并在 manifest 记录原 ref 和实际 preprobe ref；这不会给 Sub2API 账号绑定生产代理。
- Windows 客户端使用其本机代理；服务器池的 `127.0.0.1` 地址不能照抄到另一台机器。

## Bridge 和验收

- `200 + probe=passed + action=created`：客户端新增账号。
- `action=updated`：更新已有账号。
- `422`：隔离，不能算成功。
- `403`：management secret 不匹配。
- `500`：检查 bridge、Sub2API 和 PostgreSQL。

最终再调用 Grok 分组 `/v1/responses`。服务器路径以 manifest/imported IDs 为新增证据；客户端路径以 bridge `action=created` 为新增证据。

## 清理边界

- 账号删除前建立数据库恢复点，只删除用户明确指定的账号 ID。
- 删除 Sub2API 账号不自动等于删除 Mailu 邮箱；两者分别授权。
- 只删除本任务创建的临时 `config.json`、venv、任务调试端口和已证明归属的浏览器 profile。
- auth、账号密码记录、数据库备份和审计日志默认保留为受限运行产物，除非用户精确授权删除。
