# Grok Sub2API Runbook

## 目录

- 配置发现
- 服务器协议 canary
- 外部 Windows 客户端
- OAuth 恢复
- 账号状态
- Bridge 验收
- 清理边界

## 配置发现

1. 确认项目为 `/root/grok-build-auth`，但不要把该服务器路径复制到 Windows 客户端。
2. 用 `systemctl show <unit> -p FragmentPath -p EnvironmentFiles -p ExecStart` 找到实际 unit 和 env。
3. 从 env 读取 bridge/Sub2API base、Grok group 配置、数据库和 credential file；只打印变量名或脱敏值。
4. 通过 Sub2API 管理元数据核对配置的 group ID 对应 name=`grok`，不假定固定数字。
5. credential file 权限应为 `0600`，私有目录应为 `0700`；不在命令行展开密钥。

## 服务器协议 canary

1. 核对 `private/runtime.env`、Mailu、Sub2API、PostgreSQL 和注册代理池身份。
2. 运行 `scripts/check_v2ray_isolation.py` 和 `scripts/check_proxy_pool.py`。
3. 固定 `--count 1 --workers 1 --failure-policy abort`。
4. `protocol-yescaptcha` 通过 IMAP 读取验证码并调用已配置的 Turnstile provider。
5. 检查 manifest、preprobe、精确账号状态、指定账号 postimport test 和分组 postprobe。

服务器协议注册是保留方式。服务器不启动 `clients/windows/grok_register_ttk.py`；外部 Windows 客户端是另一条独立路径。

## 外部 Windows 客户端

1. 使用客户端仓库副本和本机真实代理执行 preflight；首次并发为 1。
2. full 模式可 `--skip-cdp`；export-only 才附着用户明确启动的 Edge CDP。
3. 不结束用户 Edge。验证码优先从邮件 subject 的 `XXX-XXX` 解析，填表时去掉连字符。
4. 浏览器离开 tos-gate、账号文本落盘都不是完成；必须继续 OAuth、客户端 preprobe、正式 auth 写盘、bridge push 和最终 probe。
5. device OAuth 超时可复用 SSO 走协议 OAuth，不重新注册同一账号。

`cpa_*`、`cpa_auths/`、`cpa_pending/` 和 `cpa_cooldown/` 是兼容名称。业务文档和报告使用“Sub2API auth”。

## OAuth 恢复

下列证据表示 refresh 已失效：

```text
invalid_grant
Refresh token has been revoked
GROK_OAUTH_TOKEN_REFRESH_FAILED
```

恢复顺序：

1. 核对受限产物是否保留邮箱、密码或 SSO，不打印值。
2. 有恢复凭据时重新铸造 OAuth，先直接请求官方 CLI `/responses` 验证。
3. 更新原 Sub2API 账号，不创建重复账号；执行指定账号 test 后再恢复调度。
4. 无密码但邮箱可收信时走密码恢复；无任何恢复能力时才列为逐 ID 删除候选。
5. 旧 auth 或数据库备份不能替代重新登录，但数据库备份必须保留到恢复完成。

## 账号状态

| 结果 | 含义 | 动作 |
|---|---|---|
| 指定账号 test completed | 当前真实可用 | 保持入组和调度 |
| 402/429 明确额度耗尽 | 凭据可用、额度冷却 | 保留，等待 reset |
| refresh revoked | token 不可刷新 | remint 或列为逐 ID 候选 |
| 403 entitlement/TOS | 资格传播、订阅或风控 | 等待并复测，不立即删除 |
| 网络、TLS、5xx | 结果不确定 | 修复链路后复测 |
| 未绑定分组 | 历史或中断状态 | 查导入证据，不能直接删除 |

数据库 active、schedulable 或 refresh token 字段存在都不是可用证据。审计必须调用指定账号 test；分组请求只作为整体补充。

## Bridge 验收

- `200 + probe=passed + imported=true + action=created`：新增账号完成。
- `action=updated`：更新已有账号，不能声称数量增加。
- preprobe 422 应为零写入；postimport 422 可能经历写入和回滚，必须核对响应与数据库。
- 403 表示 bridge 管理凭据或授权错误；500 检查 bridge、Sub2API 和 PostgreSQL。
- 最终调用 Grok 分组 `/v1/responses`；服务器协议路径以 manifest/imported IDs 为证据，客户端路径以 bridge 响应和账号 ID 为证据。

## 清理边界

1. 先建立数据库恢复点并验证可读。
2. 生成逐 ID 清单，包含脱敏身份、分类证据、分组、调度和恢复可能性。
3. 额度耗尽、网络错误、资格传播和归属不明账号不进入删除清单。
4. 删除 Sub2API 账号、Mailu 邮箱和 auth 文件分别授权；一个动作不自动授权另两个。
5. 已成功入库的 auth 文件只有在完成 auth-to-account 映射、指定账号验证和用户逐文件授权后才能删除。
6. 只清理由 manifest、精确路径、PID 或账号映射证明归属的目标；禁止按名称、扩展名、时间或目录类别批量清理。
