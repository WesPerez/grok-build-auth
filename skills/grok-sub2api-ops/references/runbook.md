# Windows Grok Client Runbook

## 目录

- 预检
- Full 注册 UI
- 已登录账号导出
- Bridge 与验收
- 错误索引

## 预检

1. 使用 Python 3.12 或 3.13。
2. `config.json` 从 `clients/windows/config.example.json` 创建，权限受限。
3. `cloudflare_auth_mode=bearer`。
4. `mint_required=true`、`cpa_push_required=true`、`cpa_require_probe_passed=true`。
5. 客户端本机代理监听地址可连接；不要照抄另一台机器的 `127.0.0.1:<port>`。
6. export-only 时用以下方式启动并确认 Edge CDP：

```powershell
msedge.exe --remote-debugging-port=9222
netstat -ano | findstr "LISTENING" | findstr "9222"
```

只附着用户已授权的 Edge，不结束它。

## Full 注册 UI

固定并发 1。页面变化时按以下状态推进，不按固定 sleep 猜测成功：

1. 打开 `accounts.x.ai/sign-up`。
2. 只点击有尺寸、可见的“使用邮箱注册”；OneTrust 常存在零尺寸隐藏按钮，不要硬点。
3. 填邮箱后，点击提交无效时优先在邮箱框按 Enter，再用 `form.requestSubmit()` 回退。
4. 验证码优先从邮件 subject 的 `XXX-XXX` 提取，填表时去掉连字符。
5. 资料页“完成注册”不能以 DOM 字段已填或本地账号文件为成功。至少要进入 `grok.com` 并出现 `sso`/`sso-rw`。
6. SSO 只用于 Web 会话；必须继续完成 OAuth mint、auth 写盘和 bridge push。

## 已登录账号导出

export-only 要求 Edge 中已有 `grok.com` 标签且目标身份正确。脚本会：

1. 附着 CDP 9222。
2. 选择非 `accounts.x.ai` 的 `grok.com` 标签。
3. 验证 `sso` 或 `sso-rw` cookie 名存在，不打印值。
4. 运行 device OAuth；失败时可由客户端实现回退 SSO 协议 OAuth。
5. 写 `xai-<email>.json`。
6. push bridge，并要求 `probe=passed`。

多账号 Edge 必须先人工确认当前身份，避免给错误账号授权。

## Bridge 与验收

- 200 + `probe=passed`：指定账号 probe 已通过并进入 Grok 分组。
- `action=created`：本次新增账号。
- `action=updated`：更新已有账号，不能声称账号数 +1。
- 422：probe 未在窗口内通过，账号保持隔离。
- 403：management secret 不匹配。
- 500：查 bridge journal、Sub2API 和数据库依赖。

最终最好再调用 Grok 分组 `/v1/responses`。如果没有分组 API Key，只能报告“bridge 指定账号探针通过”，不能扩大为客户端业务入口已验证。

## 错误索引

| 现象 | 处理 |
|---|---|
| CDP 9222 连接拒绝 | 用远程调试参数重启 Edge，核对监听地址 |
| 没有已登录 Grok 标签 | 在同一 Edge 登录目标账号后重试 |
| 页面没有邮箱框 | 等 SPA，点击可见“使用邮箱注册”，检查代理挑战页 |
| 邮箱提交无反应 | Enter，再 `requestSubmit()` |
| 验证码不来 | 查邮箱 JWT、subject、MX/IMAP 和 bridge 日志 |
| 资料页不跳转 | 查 Turnstile、密码规则、网络响应；不要记录假成功 |
| device 一直 pending | 确认点了“允许”并到 done 页，核对目标身份和代理 |
| push 超时 | 先查账号是否已创建，再幂等重推同一 auth |
| bridge 422 | 等资格传播或上游恢复，重推同一 auth |
| push 200 但无 `probe=passed` | 按失败处理，不能计为可用账号 |
| Responses 401/403/429 | 分别查 token refresh、entitlement/传播、额度 reset/cooldown |
