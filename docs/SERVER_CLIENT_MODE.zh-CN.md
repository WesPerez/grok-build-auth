# Linux 服务器模拟客户端模式

`server_client_mode` 用于隔离 Linux/Xvfb 服务器模拟客户端与 Windows 客户端的行为。该开关只有在当前平台为 Linux 且配置值为 `true` 时生效；Windows 即使误填为 `true`，也仍执行原有客户端路径。

## Windows 默认路径

- `server_client_mode=false`，现有 `config.json` 无需增加或修改该字段。
- 保持 `hide_window=true`、`stealth_patch=false` 和原有 JavaScript 表单、TOS、chat canary 行为。
- 只接受原有 `sso` cookie，不使用服务器模式的 `sso-rw` 兜底。
- 不启用 Linux/Xvfb 的原生 CDP 输入、点击、稳定等待和门禁恢复重试。

## Linux/Xvfb 路径

只通过 `scripts/run_linux_client_full.py` 启动。runner 会在每个隔离 route 的配置中写入：

```json
{
  "server_client_mode": true,
  "native_registration_interactions": true
}
```

该模式使用 headed Edge、原生 CDP 表单和 TOS 交互、自然算术 chat canary、门禁回退恢复及 `sso-rw` 兜底。每路使用独立代理、运行目录和浏览器实例。

单账号 canary 通过后才能扩大到两路。成功必须同时满足本地 auth 已生成、bridge 返回 `action=created`、`probe=passed` 和精确账号 ID；pending、cooldown、已有账号或仅网页注册完成均不计入目标数。

服务器模拟客户端运行期间不得并发启动 `scripts/register_and_import.py`。
