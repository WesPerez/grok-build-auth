# 上传服务器后从这里开始

本目录是 `grok-build-auth` 上游提交
`9f94e5b4c78272b06bd1899d648854db279924d2` 的服务器交接包。

## 交给服务器 Agent 的方式

让服务器 Agent 先读取：

1. `START_HERE.zh-CN.md`
2. `SERVER_AGENT_HANDOFF.zh-CN.md`
3. `SERVER_AGENT_PROMPT.zh-CN.txt`
4. `README.md`
5. `SECURITY.md`
6. `NOTICE`

然后把 `SERVER_AGENT_PROMPT.zh-CN.txt` 的内容作为任务要求执行。

## Agent 要完成的工作

1. 核验压缩包和源码，不运行真实认证链路。
2. 在本目录初始化 Git，或导入用户自己的私有仓库。
3. 建立 `hardening/server` 分支。
4. 完成手册第 3 节的全部安全修改。
5. 生成依赖锁文件、无秘密配置模板和测试。
6. 安装到独立系统用户及受限目录。
7. 提交源码差异、测试结果、安装和回滚命令。
8. 在任何真实账号创建、OAuth 授权、验证码求解或额度探测前，向用户做最终确认。

## 用户可能需要提供的内容

仅在对应路径确实需要时提供，且不得发送到聊天、日志或 Git：

- 自有 x.ai/Grok 账号的授权确认。
- CLIProxyAPI 的实际 auth 目录。
- 可信代理配置（若服务器网络需要）。
- 完整注册路径才需要的 YesCaptcha、Tempmail 或 Cloudflare 凭据。

推荐默认使用用户已有账号的 OAuth 路径，不运行批量注册、临时邮箱或自动验证码链路。

## 重要限制

- 本项目不是 Web 服务，不需要 Nginx 或公网入站端口。
- 不要直接运行未经加固的 `run.py`。
- 不要使用 `-n` 大于 1、`-t` 大于 1，也不要建立自动定时任务。
- 不要运行 `xai_build_quota_probe.py`，除非用户明确允许真实请求和少量额度消耗。
- auth JSON、`.env`、密码、SSO、token、cookie 和验证码不得进入 Git 或普通日志。
