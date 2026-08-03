# Grok OAuth 与 Resin 池运维判断手册

本文总结 Grok OAuth 账号池、Sub2API 调度和 Resin 动态出口联动时可复用的判断规则。目标是先回答“哪一层坏了”，再选择最小动作，避免把代理故障、额度、OAuth revoked 和路由超时混为一谈。

## 1. 三层状态必须分开

一个账号至少有三层状态：

1. **凭据层**：access token、refresh token、subject 和官方 base URL 是否完整，refresh 是否被 xAI 接受。
2. **Sub2API 调度层**：账号是否 active、schedulable、只绑定目标分组，以及是否处于 rate-limit、overload 或 temp-unschedulable 窗口。
3. **Resin 出口层**：账号绑定的逻辑代理是否存在，sticky lease 当前物理节点能否连接 `grok.com` 和 `auth.x.ai`。

因此：

- `active + schedulable` 是必要条件，不等于“此刻一定能被选中”；还要排除当前 cooldown 和临时退避。
- TLS/CONNECT 通过只证明出口链路，不证明 OAuth、额度或模型调用可用。
- 一次 Grok 分组请求成功只证明本次被选中的账号和路由成功，不证明池中每个账号都成功。
- Router fallback 成功不能反证 Grok 分组成功，必须从日志确认 provider、group、account 和无 fallback。

## 2. OAuth 生命周期的正确解释

- access token 的小时级过期与 refresh token revoked 是两件事。access token 过期后应由唯一所有者 refresh；不能把 access 过期当坏号。
- Sub2API 接管 refresh 链后，本地 `xai-*.json` 只是交接快照。旧快照批量重推可能覆盖已轮换的新 refresh token。
- 同批 token 的 `expires_at` 接近，会让后台刷新器在同一检查波触发，看起来像账号同时掉线；这只解释触发时间，不解释 revoked 根因。
- `invalid_grant` 或明确 revoked 只能证明旧 refresh 链被上游拒绝。没有 token-version、请求 ID 和持久化日志时，不能断言账号被封，也不能断言一定是旧文件覆盖。
- `EOF`、SOCKS failure、TLS timeout 和普通 5xx 属传输不确定。它们可以让账号进入短暂 refresh 退避，但不能据此删除账号。
- `ops_error_logs.upstream_errors` 可能在一行中保存一次 failover 的多个账号错误。revoked 归因必须使用数组元素自己的 `account_id`；外层 `ops_error_logs.account_id` 可能是同一请求的后续候选。不能先全文匹配 revoked，再把外层 ID 当坏号。

## 3. 503 的定位顺序

`No healthy Grok OAuth account is currently available` 表示请求在给定的凭据切换预算内没有拿到可用账号，不等于全池 refresh token 都丢失。按以下顺序核对：

1. 请求是否进入预期 Grok group。
2. 首个候选是在 refresh 前失败、refresh 传输失败、明确 revoked，还是上游模型返回错误。
3. 后续候选是否只是撞到共享 failover timeout。
4. Router 是否随后切到其他 provider；客户端最终 200 可能来自 fallback。
5. 同时查看账号总数、永久 error、当前 429、temp-unschedulable 和实际可选数，不只看 UI 的一个计数。

## 4. revoked 恢复的证据等级

恢复尝试必须有界，并区分结果：

| 证据 | 可得结论 |
|---|---|
| 登录成功、consent 返回 `Access denied` | authorization-code 授权被业务层拒绝；不是 Resin CONNECT/TLS 故障 |
| device endpoint 签发 code | device grant endpoint 可达；账号尚未授权，不能算恢复 |
| device flow 得到 access/refresh token | 只算 mint 成功；仍需 identity、bridge update 和指定账号探针 |
| 只有 `oauth_runtime_error/unknown` | 诊断不足；不能写成账号级拒绝，也不能写成代理故障 |
| bridge `action=updated`、原 ID、probe passed | 候选凭据已通过账号级验证；批量 reverify 后才能恢复调度 |

device fallback 只记录脱敏阶段和终态枚举，例如 `browser_started`、`device_login`、`device_consent`、`token_received`，以及 `access_denied`、`approval_timeout`、`transport_error`。不得记录 device code、URL query、账号或 token。一个 canary 失败不能自动推断另一个账号也失败。

## 5. Resin 的“每账号一个节点”到底是什么

生产设计保证的是：

```text
Sub2API account
  -> 唯一 proxy_id
  -> 唯一 GrokEU.shard-N 逻辑 Account
  -> 一个 sticky lease
  -> 当前某个物理节点/公网出口
```

这不等于永久独占一个物理节点：

- Resin 可以让不同逻辑 Account 落到同一物理节点，尤其是可信兜底池容量较小时。
- 节点熔断、订阅刷新或 lease 到期后，同一逻辑 Account 可以迁移到新物理节点。
- `max_per_egress=1` 约束的是发布到受管订阅的候选选择，不自动成为 Resin 的全局“一个 lease 独占一个 egress”约束。
- 判断隔离时必须分别统计 Sub2API `proxy_id/username` 唯一性，以及 Resin `leases.node_hash/egress_ip` 的实际共享分布。

如果业务硬性要求物理出口独占，需要 Resin 明确支持排他租约或另建分片容量策略；不能靠批量删除 lease 假装达成。无此能力时，准确表述应是“账号拥有独立逻辑身份和粘性租约”。

## 6. 代理来源与日更

- GitHub Raw 的公开配置只是候选来源，不是“节点都可用于 Grok”的证明。
- 本机必须重新验证代理认证、`grok.com:443`、`auth.x.ai:443`、TLS 证书和小流量出口；所有目标都通过才准入。
- 非论坛 source 避免七天帖子过期，但仍可能整批变质。日更必须 fail-closed：新批低于绝对阈值或相对上一批骤降时保留旧池。
- 两阶段 bridge 发布必须保留上一代：刷新、重启、复验、Resin 同步全部成功后才 commit；任一步失败都 rollback。
- systemd 的失败回滚命令不能用 `-` 前缀吞掉非零退出；否则 staged 代可能残留而 unit 仍显示成功。维护器只需读取 `/root` 下脚本并把状态写入显式 `/var` 路径时，保持 `ProtectHome=read-only` 和空 capability，不为路径便利放宽整个 home 或 DAC 能力。
- trusted pool 的名称不是健康证明。只有在同一周期接受 `grok.com` 与 `auth.x.ai` 等价验证的订阅才可进入生产 Platform；否则保留对象用于人工回滚，但从生产过滤器移除。同步器若默认兼容保留 trusted 过滤器，生产配置必须使用显式开关关闭该行为并在应用后读回核验，不能只看配置文件。仍应同时报告 managed/trusted 的 lease 分布和共享程度。
- 同一账号重复出现带明确 `auth.x.ai/oauth2/token` endpoint 的 EOF、TLS handshake timeout 或 SOCKS server failure，可以作为精确轮换该 sticky lease 的被动证据；没有 endpoint 的笼统 refresh timeout 证据不足。轮换只删 lease，不测试账号、不清 429、不改 `proxy_id`，并保留批量故障全局保护。

## 7. 最小验收策略

不应对数百账号逐个执行 Test Connection 或模型生成。推荐四步：

1. **全量结构核验，无生成**：账号 status/schedulable、凭据字段、subject 唯一性、官方 base、唯一目标组、唯一逻辑 proxy、proxy active。
2. **全量即时调度核验，无生成**：分别统计 429、overload、temp-unschedulable，并按 revoked、额度、传输分类。
3. **代理池验证，不使用账号**：运行 CONNECT/TLS/trace 报告，确认 `grok.com` 与 `auth.x.ai` 都通过且安全阈值满足。
4. **一个官方客户端烟测**：使用当前官方 Codex CLI、`grok-4.5` 和 `high`，输入自然的小型结构任务；从服务端日志确认 Grok group/provider/account HTTP 200 且没有 Router fallback。

最终报告必须准确区分：

- **结构健康**：全量账号记录、凭据和逻辑绑定符合约束。
- **当前可选**：排除 429 和临时退避后此刻能被调度的数量。
- **已实测**：真实 probe 覆盖了哪些账号或仅覆盖分组一次。
- **未证明**：未逐号生成，因此不能宣称每个账号都完成了实时模型调用。

## 8. 删除与清理

- revoked 删除前先冻结精确 ID、脱敏身份、分组、调度和最后错误证据，并建立 custom-format PostgreSQL 备份后执行 `pg_restore -l`。
- Admin DELETE 后逐 ID GET 必须为 404，组绑定必须为 0。
- 账号、Sub2API proxy、Resin lease、邮箱和本地恢复材料是不同对象，分别核对引用和授权。
- 只有 proxy 不再被任何 active 账号或 fallback 引用时才可精确删除；只有确认逻辑 Account 后才可删除对应 Resin lease。
- 研究下载、浏览器 profile、Xvfb 和临时日志只按本任务直接归属清理；长期恢复点和失败恢复材料按明确保留策略处理。

## 9. 收口清单

- 账号总数、active、永久 error、429、临时退避和当前可选数已分别记录。
- 所有 live 账号只在目标分组，priority 语义与 account-group priority 没有混淆。
- 所有 live 账号拥有唯一 active 逻辑 proxy；物理 lease 共享分布已如实报告。
- 最新 Resin 批次同时验证两个 xAI 目标并满足 fail-closed 阈值。
- 删除、proxy/lease 清理、恢复点和保留材料均有精确结果。
- 官方 Codex 烟测和服务端无 fallback 证据已落盘或在报告中引用。
- 源码、技能、调度模板测试通过；敏感扫描和远端 SHA 核对完成。
