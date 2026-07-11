# GROK 全面审查与改进建议

日期：2026-07-12

## 结论

当前 GROKAUTH + Sub2API 主链的职责划分总体正确：GROKAUTH 负责注册、SSO、OAuth 和凭据生成，Sub2API 负责账号导入、调度、刷新和请求代理。不要整体替换为外部注册机。

应优先修复核心正确性，再吸收外部项目的工程优点。最高优先级是：检查邮箱 RPC 业务结果、解析建号 Server Action 业务结果、自动执行真实上游探针。动态 IP 和验证码优化排在这些正确性门禁之后。

## 动态节点与 IP

### 当前状态

- GROKAUTH 只读取一个全局 `HTTP(S)_PROXY`，所有账号共用，没有节点池、按账号选点、出口 IP 探测或 IP 记录。
- Sub2API 支持账号绑定静态 `ProxyID`，但生产 4 个 Grok 账号目前都未绑定代理，实际共享服务器出口。
- `grok_bytao` 为每个账号生成不同 Resin session/account，并给 Chromium 建认证代理桥。这一思路有价值，但不能直接复制：它没有验证不同 session 是否真的对应不同出口；CPA/OIDC mint 未继承注册时的 runtime proxy；代理失败还会回退直连。

### 推荐设计

实现账号级 `ProxyContext`，而不是每个请求随机换 IP：

1. 每次注册尝试从受控节点池领取一个 sticky lease。
2. 注册前探测出口 IP、地区和连通性；记录 `proxy_id`、地区、出口 IP hash 和健康结果，不保存代理密码。
3. warm-up、邮箱 RPC、Turnstile、建号、SSO、OAuth、token exchange、首个上游 probe 全程固定同一出口。
4. 下一个账号领取不同节点；节点失败按类型熔断。
5. 代理不可用必须 fail closed，禁止静默直连。
6. 导入 Sub2API 时把该账号映射到对应 `ProxyID`，后续 refresh 和业务请求继续保持网络身份一致。

## 验证码

### 已确认的三种路径

- 当前 GROKAUTH / `grok-build-auth`：付费 YesCaptcha，默认 `TurnstileTaskProxylessM1`，每次注册至少一次；OAuth 成功复用注册 SSO/cookie 时可免第二次，否则再次付费。
- `grok_reg-share` / `grok_bytao`：有头 Chromium + `turnstilePatch` + 页面真实点击/读取 token，不依赖付费打码，但依赖浏览器环境和页面结构。
- 邮箱 OTP：各项目通过临时邮箱、自建 Cloudflare 邮箱或 IMAP 拉信，不属于付费验证码打码。

### 当前风险

- YesCaptcha 使用 Proxyless task，没有绑定注册代理，可能造成求解环境和账号注册出口不一致。
- solver endpoint 和 premium task 基本写死，缺少 provider 抽象、成本指标和备用服务。
- sitekey 固定，Castle token 未实现且传空。
- 浏览器方案虽然可免付费，但页面变化、扩展失效和并发资源消耗较大，不适合替代协议主链的默认实现。

### 推荐策略

- 抽象 `CaptchaProvider`：endpoint、task type、代理模式、超时、重试、错误码、成本统计均可配置。
- 保留 YesCaptcha 为稳定默认或显式后备；服务支持代理任务时绑定账号的 sticky proxy。
- 动态抓取 sitekey；Castle challenge 出现时显式失败或进入受控浏览器/人工兜底，不能继续传空并假定成功。
- 浏览器真实交互作为可关闭的备用路径，不把“免费绕过”当核心正确性依赖。

## 核心正确性问题

### P1：已修复

1. 邮箱发码、校验码和密码校验现在会检查 gRPC-web 业务状态，失败立即停止。
2. 建号现在会排除明确 RSC 业务错误，并要求 SSO JWT chain 或有效 session cookie 成功证据，不再仅凭 HTTP 200。
3. 导入前真实上游探针与导入后 Sub2API 探针仍需变成自动门禁；现有单轮和多轮 200 是运行证据，不是每批自动保证。

### P2：随后修

- resume 应限制 auth 文件必须位于当前 batch 目录，并校验每个 auth SHA256。
- Sub2API 收口校验应确认 token 关键字段、过期时间和账号身份仍存在，并用测试确认 credentials 更新是 merge 而非覆盖。
- 对邮箱 RPC、solver、OAuth token exchange 做有限、分阶段、幂等感知的重试；建号和导入在重试前先查状态。
- 增加注册 gRPC、RSC 业务错误、SSO/OAuth、代理粘性、代理失败不得直连、CaptchaProvider 和真实 Sub2API contract 测试。

## Sub2API 风险

以下生产数量和环境变量是 2026-07-12 的只读快照，不是源码的永久事实。

- 生产 `XAI_ALLOW_UNSAFE_URL_OVERRIDES=true` 全局绕过 host/private-network 保护，有 SSRF 风险。应只 allowlist 内部 `grok-cli-proxy` 或使用受控内部代理标识。
- `XAI_GROK_UNSAFE_ALLOW_CONCURRENCY_GT_ONE` 目前没有真正执行限制；生产有一个账号 concurrency=10。Grok OAuth 账号默认应强制为 1。
- RefreshToken 指定不存在代理时会静默直连，应改为明确失败。
- OAuth session 只存在进程内存，重启或多副本会丢失；应迁移 Redis，并绑定管理员、代理和 redirect URI。
- 固定 `grok-cli/0.2.93` 头会老化；需要版本管理和兼容测试。

## 值得吸收的精华

- `grok_reg-share` 的注册与 mint 有界队列、每线程隔离 Chromium、cookie/SSO 复用、device token polling 有限重试。
- `grok_bytao` 的账号级动态代理 session 概念、认证代理桥、Cloudflare 邮箱兼容和远程管理 API 上传，但必须补全链路代理继承、出口验证、TLS/allowlist、幂等和审计。
- `grok-build-auth` 的纯 HTTP 协议链、动态 Next.js action 抓取、SSO 快路径和 PKCE OAuth。
- CPA auth 的 schema 校验、`refresh_token` 必需、固定 Build 通道、临时文件 + `fsync` + `os.replace` + `0600` 原子写。

## 不应照搬

- 不要按请求随机换 IP；账号内部网络身份漂移比单出口更危险。
- 不要允许代理失败自动回退直连。
- 不要整体用 `grok_bytao` 覆盖 `grok_reg-share`，前者代理功能更新，但 OIDC 容错、Cookie 弹窗消歧、截图和网络重试更旧。
- 不要把浏览器 Turnstile patch 当唯一方案，也不要把付费 Proxyless solver 当作无风险方案。
- 不要明文记录 OTP、邮箱密码、SSO 或 token 前缀。OAuth auth 文件已实现 `0600 + fsync + os.replace + 目录 fsync`，应保持这一属性。

## 落地顺序

1. 已修复邮箱 RPC 和 RSC 建号成功判定。
2. 已修复 Grok/Codex 多轮 Responses `content:null` 兼容，且限定在指向 compat sidecar 的 `grok-4.5` 账号。
3. 把导入前直连 probe、导入后 Sub2API probe 变成强制门禁。
4. 在阶段耗时、CPU/RAM 和失败率可观测后，才引入默认 1、最大 2 的有界协议注册并发；OAuth 和浏览器路径保持串行。
5. 实现 per-account sticky `ProxyContext`，全链路传递、出口验证、fail closed，并在导入时绑定 Sub2API `ProxyID`。
6. 先抽象 `CaptchaProvider`，再把付费 solver 作为默认、浏览器 Turnstile 作为低并发 canary 后备；页面只显示后端真实可用的方式。
7. 加固 resume/auth 完整性、Redis OAuth session 和 URL allowlist，建立 1 账号完整 canary 后再放量。

## 2026-07-12 实施状态

- 协议注册已支持有界并发，生产默认最大 2 路；真实 3 账号测试中，前两项并发 43 秒完成，第三项 22 秒完成。
- 已实现账号级 sticky proxy pool schema、租约容量、代理密钥环境变量引用和 fail closed。当前未配置多节点池，仍使用 `runtime.env` 中的单一 sticky SOCKS 代理，不声称已实现不同出口 IP。
- 已实现 `CaptchaProvider` 和 `RegistrationBackend`。协议模式使用 YesCaptcha ProxylessM1；浏览器模式使用 Playwright + 系统 Edge 完成整页注册，固定 1 路并发，代理失败不回退直连。
- 浏览器后端已执行真实 canary：补齐 `socks5h` 兼容、本地 V2Ray 浏览器入站、Xvfb 有头 Edge、新版“Sign up with email”入口和 OTP 页面等待。真实流程已进入邮箱 OTP 阶段，但当前代理出口曾被 Cloudflare 拦截，随后 x.ai 又出现 120 秒不发码，未完成建号。因此它仍是可选 canary，不作为生产默认链路。
- 对照外部项目后采用了有头 Chromium、独立 context、真实 DOM Turnstile、Cookie 交接和失败回收；未照搬其代理失败回退直连、OIDC 改走 direct 和未验证出口的 Resin session 方案。
- 当前服务器只有一个 Trojan 代理出口，且实测出现 TLS EOF 和连接超时；Sub2API `proxies` 表为空。动态多 IP 代码已就绪，但生产启用仍缺少至少第二个独立、可验证的外部代理上游。
- YesCaptcha 控制面请求现在禁用环境代理，避免 Proxyless solver API 被错误送入账号 SOCKS 出口。
- 已加入导入前逐 auth Grok CLI 探针和导入后 Sub2API 分组探针。HTTP 403 账号会被隔离，不进生产调度池。
- 已加入 OAuth 恢复工具，对“建号成功但 OAuth 失败”的批次使用原邮箱/密码继续协议 OAuth，必要时使用同代理的 Edge fallback，不重复建号。
- 真实恢复结果：原 6 账号批次中 3 个旧误判账号全部补出 OAuth auth，2 个探针 200 并导入，1 个探针 403 隔离。另有 1 个完整 canary 通过注册、OAuth、预探针、导入和后探针。
- 真实 3 账号并发批次的注册、SSO 和 OAuth 都成功，但 3 个 Grok Build 预探针均返回 HTTP 403，已全部隔离且未导入。

## 验证与覆盖限制

- 实施时已用项目完整测试集和真实 canary 验证主链；具体测试数量以当前 CI 结果为准。
- `grok-build-auth`：`python3 -m compileall -q .` 通过。
- 三个浏览器项目的部分单测因服务器缺少 `tkinter` 无法导入；`grok-auto-register` 的 3 个纯文本转换测试通过。
- `grok.txt` 共 1301 行，已完整读取；其中保存了 6 个 LINUX DO 主题的可见正文，但多个主题明确显示“加载更多帖子”。
- 当时的 reader 尝试对 6 个主题均只返回私有/不存在的 404 外壳。因此未加载楼层无法补齐，不能声称论坛全部楼层已读。
- 4 个 GitHub 仓库已浅克隆并按源码审阅：AaronL725/grok-register、Git-creat7/grokRegister-cpa、dongguatanglinux/grok-build-auth、maxucheng0/grok-auto-register。

## Codex 多轮 Responses 的 `reasoning.content=null` 兼容问题

### 已证实根因

Codex 第一轮请求可成功；第二轮将上一轮 reasoning item 回放到 `input` 时，可能包含：

```json
{
  "type": "reasoning",
  "content": null,
  "encrypted_content": "...",
  "summary": []
}
```

Grok CLI `/v1/responses` 不接受 reasoning item 中显式存在的 `content: null`，会返回 HTTP 422：`Failed to deserialize ... ModelInput`。A/B 验证表明，仅删除该空字段即可恢复第二轮；`encrypted_content`、`summary`、`id` 和 metadata 不是本次报错根因。

`content: null` 不是“思考程度为 NULL”。思考强度由顶层 `reasoning.effort` 控制；多轮思考上下文主要由 `encrypted_content` 和 `summary` 携带。删除这个非法空字段不会关闭后续思考。

### 外部项目与论坛覆盖

- 四个已审阅的 Grok 注册项目、三个归档和当前可见的 LINUX DO 正文都没有实现或讨论该 sanitizer。它们的 `/responses` 探针都是固定字符串 `input` 的单轮请求，不回放 Codex 历史。
- 决定性的成熟实现在它们关联的 CLIProxyAPI：`router-for-me/CLIProxyAPI` 提交 `ddd10539adf3fea72c9ab23d20c5f97dc8d6602c`（2026-05-16）新增 `normalizeXAIInputReasoningItems`，对 xAI `input[*].type == reasoning` 精确删除值为 `null` 的 `content` 和 `encrypted_content`，保留 `summary`，并有与本次复现结构一致的回归测试。
- 该修复已进入 CLIProxyAPI `main` 并包含在 `v7.2.66` 中。Issue `#3704` 也记录过同样的 `ModelInput` 422，但其具体触发项是 custom tool/history，不能与本次的唯一根因混为一谈。
- `grok.txt` 的六个 LINUX DO 主题只能核验已保存的可见楼层；论坛 reader 对未登录请求返回 private/404 外壳，因此不声称未加载楼层已读完。已读正文中无 `content:null`、`ModelInput` 或 HTTP 422 的同类讨论。

### 对当前架构的意义

- 当前 Nginx Grok sidecar 只补 CLI 请求头并透明转发 body，无法修改 JSON，所以它能解决早先的 header/402 问题，不能解决本次 422。
- 不必改动 Sub2API 的通用 OpenAI 路径。最小、低影响的修复位置是 Grok 专用 `patchGrokResponsesBody`，仅删除 reasoning item 中的 `content:null`，并保留其他字段。这不影响其他分组或模型。
- 若坚持 Sub2API 源码零改动，只能换用已含该修复的 CLIProxyAPI 转发链，或把 sidecar 升级为能解析和重写 JSON 的应用代理。后者的实现、测试和运维风险都高于 Grok 分支内的局部 sanitizer。

## 可复核来源

- AaronL725/grok-register：`c6a82cde9498`
- Git-creat7/grokRegister-cpa：`2d7a59e06b6f`
- dongguatanglinux/grok-build-auth：`9f94e5b4c782`
- maxucheng0/grok-auto-register：`20fc2f39ea15`
- router-for-me/CLIProxyAPI：修复提交 `ddd10539adf3fea72c9ab23d20c5f97dc8d6602c`，已包含于 `v7.2.66`。
- LINUX DO topics：`2556512`、`2558107`、`2560435`、`2561596`、`2562322`、`2564994`。
- 审查归档 SHA256：`2ce366e2c9185062f192dd6f1e090c923ff825f474e6a6d8bb8da0b78e82a5b3`、`bb4ff06d91f47e91533aeaec017367a3fb249624769d6edd84d2c9975ad1d314`、`ed64400831565202d3183a4e859ac45c77d849b302b61df533b27a7d125c8dac`。

项目内实施证据见 `run.py`、`scripts/register_and_import.py`、`xconsole_client/`、`PROXY_POOL_OPERATIONS.zh-CN.md` 和 `SUB2API_GROK_RESPONSES.zh-CN.md`。原始归档、论坛抓取和第三方仓库 clone 不作为本仓库产物保留。
