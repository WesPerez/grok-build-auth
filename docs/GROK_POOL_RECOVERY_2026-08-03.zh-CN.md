# Grok 账号与 Resin 池恢复复盘（2026-08-03）

本文记录本轮从“Grok 分组全部不可用”到生产收口的事实、改动和可复用判断。敏感 token、邮箱、代理凭据和完整出口地址不进入文档。

## 1. 最终问题不是单一故障

本轮同时存在四类现象，不能用一个“账号全挂”概括：

1. 两个账号有明确 refresh revoked 证据，且现有恢复材料无法完成可验证 remint。
2. 一批正常账号处于真实 `429` 额度冷却，凭据仍应保留。
3. 多个账号的 refresh 在 `auth.x.ai/oauth2/token` 前发生 EOF、TLS handshake timeout 或 SOCKS server failure，属于代理传输故障，不是账号 revoked。
4. 单次请求连续遇到传输失败后耗尽 15 秒共享 credential failover budget，Sub2API 返回 `503 No healthy Grok OAuth account is currently available`；Router 随后切到 OpenAI，因此客户端最终 `200` 不能证明 Grok 分组成功。

## 2. 账号处置

- 初始审计为 313 个 Grok OAuth：311 个结构健康，2 个明确 revoked 且不可调度。
- 写前建立 PostgreSQL custom-format 恢复点，记录 SHA256，并通过 `pg_restore -l`。
- 两个 revoked 账号只按冻结 ID 通过正式 helper 软删除；逐 ID Admin GET 为 `404`，组绑定为 0。
- 最终复核曾因两条 `ops_error_logs` 外层账号记录包含 revoked 文本而出现疑似 live 风险；展开 `upstream_errors` 后确认 revoked 数组元素分别属于已删除的 `101136`、`101159`，外层 `100907`、`101085` 是同一 failover 的后续传输失败候选。以后必须按数组元素 `account_id` 归因，不能按整行文本归因。
- 只在确认无 live 引用后删除对应 Sub2API proxy，并删除对应 Resin sticky lease。邮箱和历史恢复材料没有随账号删除。
- 最终 live 集合为 311 个；全部 `active + schedulable`、只绑定 Grok 分组、priority 一致、官方 base URL、subject 唯一、token 字段完整，并各有唯一 active `proxy_id` 和 `GrokEU.shard-N` 逻辑身份。

删除、proxy、lease、邮箱和本地 auth 是四种不同授权对象。以后不得因为其中一个已获授权就连带清理其余对象。

## 3. revoked 恢复尝试给出的证据

- authorization-code 路径完成登录后在 consent 业务层得到 `Access denied`。这证明授权被业务层拒绝，不是 CONNECT/TLS 故障。
- device-code endpoint 可达或签发 code 只证明流程入口存在，不能算 OAuth 已恢复。
- device flow 只有在得到 token、身份匹配、bridge `action=updated`、原 ID 和指定账号语义 probe 均通过后才算成功。
- 只有 `oauth_runtime_error + progress_stage=unknown` 属诊断不足，不能据此删除账号或推断整个批次结论。

为此，协议客户端改为解析实时 consent HTML/Flight 中的 principal、router state、deployment 和 Server Action 信息；Server Action 只提交一次，禁止失败重放。新增 device-code 浏览器 fallback 只保存脱敏阶段与终态枚举。bridge 更新账号时按 name、email、subject 精确解析，歧义即停，并保留原显示名与 priority。

## 4. 旧“6 个节点”的来源与局限

本机可证明的历史是：

- `/etc/v2ray/grok_pool.json` 保存了 8 路本地端口映射；当前 Resin 静态解析结果只有 6 个物理节点。
- 2026-07-14 的私有导入材料注明来源为该 V2Ray 配置与项目私有代理配置。
- 没有可审计的 source manifest 能证明这批节点来自哪个论坛。因此不能把“之前 6 个代理来自论坛”写成事实，也不能反向声称其最初一定不是论坛发现。

它们长期存在的直接原因是静态配置被本机持久化，不受论坛帖子“7 天有效期”自动控制；但“配置一直存在”不等于“两个 xAI 目标一直可用”。本轮按当前生产标准复测 8 路本地出口，仅 2 路同时通过 `grok.com`、`auth.x.ai` 和小流量出口探针。旧探针只覆盖部分目标、Resin 被动状态未把 TLS 失败计入节点 failure，以及 sticky lease 长期保留，共同造成了“看起来一直有效”的错觉。

## 5. 从论坛发现切换到非论坛候选

旧凌晨任务确实在 03:48 运行，但发现阶段得到 `selected=0`、`passed=0`，低于安全阈值，因此 fail-closed，没有覆盖旧生产池。定时器运行不等于节点池更新成功，必须检查该次 run 的终态。

生产候选后来切到无凭据的 GitHub Raw：`0xRadikal/Free-v2ray-Configs`。这只是候选来源，不是可用性背书。正式链路为：

1. 拉取 sing-box 候选并固定本次解析地址。
2. 先复测现有最多 400 个固定 bridge 槽位，只填补失败、重复出口或过度共享凭据的槽位；400 是备用容量上限，不要求一次补满。找到几个替代就只换几个，未补上的坏槽保留原端口并由 Resin 熔断隔离。
3. 常驻 bridge 重启后再次验证。
4. 每个候选必须通过代理认证、`grok.com:443`、`auth.x.ai:443`、TLS 证书和 Cloudflare 小响应出口检查。
5. 至少 320 个通过且满足相对上一批阈值，才备份 Resin SQLite 并原子更新。
6. 任何阶段失败都保留旧代或 rollback；成功后才 commit。

本轮成功批次均从 400 个候选中选择 320 个；不同时间复测的通过数会变化，证明公开节点仍会退化，不能把某次 320 当作永久库存。

## 6. trusted 池暴露出的兼容陷阱

旧 `trusted-grok-local-pool` 名称不能替代健康验证。本轮只 2/8 通过双目标探针，且多个 refresh timeout 集中在同一旧出口。

最初把生产配置改为 managed-only 后，正式同步器仍保留了现有 trusted alternation，因为它有兼容逻辑。配置文件正确不代表实际 Platform 已改变。最终增加显式 `preserve_existing_trusted_filters=false`，重新同步并从 Resin 读回 `^managed-grok-public-pool/` 后才算生效。

旧 trusted subscription 对象仍保留为人工恢复材料，但不再参与生产路由。过滤器切换不会立即删除所有历史 sticky lease；必须观察旧 lease 是否仍被沿用，并只对实际落在已排除 subscription node hash 上的 lease 做精确迁移。迁移不调用模型、不修改账号、不清 `429`，并由写前 SQLite 恢复点覆盖。

## 7. “每账号一个节点”的准确含义

当前可证明：

```text
Sub2API account
  -> 唯一 proxy_id
  -> 唯一 GrokEU.shard-N 逻辑 Account
  -> 一个按需建立的 sticky lease
  -> 当前物理 node / egress
```

不能证明或承诺 311 个账号永久独占 311 个公网 IP。`max_per_egress=1` 只约束本次发布候选；Resin 的历史 lease、节点迁移和分配策略仍可能让多个逻辑账号共享一个物理出口。物理排他必须由 Resin 提供明确排他租约能力，不能靠无界批量删除 lease 制造短暂的一对一结果。

因此全量验收分为：311 个逻辑绑定结构核验、当前 lease 分布统计、代理池无账号双目标验证，以及一次官方 Codex 端到端烟测。没有必要对 311 个账号逐个生成内容。

## 8. 自动恢复规则

- 明确 `429`：保留账号、cooldown 和当前额度证据；禁止为了测试而调用模型。
- 明确 revoked：有唯一完整恢复材料才 remint 原 ID，否则按冻结 ID 和恢复点处置。
- 重复 `auth.x.ai/oauth2/token` EOF、TLS handshake timeout 或 SOCKS server failure：只删除该 shard sticky lease；不改 `proxy_id`、不清 cooldown、不调用账号 test。
- 没有明确 endpoint 的笼统 refresh timeout：证据不足，不自动轮换。
- 同 shard 三个独立请求出现 `502/504`：可作为另一路 lease 轮换证据。
- 短窗口候选超过全局阈值：视为池级或上游故障，停止逐号 churn，先修复来源或 Platform。
- Resin 内建主动探测每小时覆盖全局节点到 `auth.x.ai` 的公开 OIDC 端点；GitHub 主池每 3 小时执行双目标全量复测并只替换坏槽。主链失败才启用论坛 fallback，未过期批次直接复用且论坛发现最多每天一次。
- 没有实际补洞时不发布新 generation、不重启 bridge。节点离开可路由视图或出口漂移后，旧 sticky lease 会在下一次请求自动迁移，无需改 Sub2API `proxy_id`。

## 9. 最终验收口径

最终结论必须同时给出四个数字或分类：

- **结构健康**：live、active、schedulable、组、priority、凭据字段、唯一 subject、唯一逻辑 proxy。
- **当前调度窗口**：真实 429、temp-unschedulable、overload，以及排除这些状态后的当前可选数。
- **代理健康**：最近正式 run 的 input、passed、selected、unique egress、实际 Platform filter 和恢复点。
- **真实调用覆盖**：官方 Codex CLI 是否经 Grok group/provider 返回 200，是否出现 Router fallback，以及本次实际选中的账号。

一次组烟测只证明本次路径，不证明 311 个账号逐号完成了模型调用；反过来，某次 503 也不证明 311 个 refresh token 全部失效。

## 10. 本轮最终生产快照

2026-08-03 22:03（Asia/Shanghai）的只读终态为：

- 311 个 live Grok OAuth 全部 `active + schedulable`，全部 priority=5、只绑定 group 5、使用官方 `cli-chat-proxy.grok.com/v1`，subject、active `proxy_id` 和 `GrokEU.shard-N` 逻辑身份均为 311 个唯一值。
- 永久 error=0、overload=0；28 个账号处于有结构化 `status_code=429` 证据的正常额度冷却。当前 2 个 temp-unschedulable 全部包含在这 28 个额度账号内，非 `429` 的 temp=0；当前立即可选 283 个，正好是 `311-28`。
- 21:08、21:09 和 21:32 的正式 transport recovery 只按重复 `auth.x.ai/oauth2/token` EOF/TLS 证据轮换了 `101055`、`101064`、`101115`、`101116`、`101003` 的 sticky lease。前四个在 21:12 refresh 成功；`101003` 的替换出口首次仍 TLS timeout，恢复器按 60 分钟 cooldown 禁止连续 churn，随后该账号在 22:02 自然出现 `token_refresh.account_refreshed`。全程没有调用指定账号 test、没有修改 OAuth、`proxy_id` 或 `429`。
- 上述 5 个恢复账号当前落在 5 个不同 node/egress；全池按需建立的 267 个 lease 只对应 214 个物理节点、213 个出口，52 个物理节点组和 52 个出口组存在共享，最大每组 3 个账号。因此只能证明每账号独立逻辑身份，不能声称 311 个物理出口独占。
- Resin Platform 实际 filter 仅为 `^managed-grok-public-pool/`。最新正式批次输入 400、双目标通过 329、选择 320、唯一出口 329；订阅解析 374 个节点、363 healthy，Platform 可路由 361。旧 trusted 订阅仍保留为人工回滚材料，但不参与生产路由。
- 官方 `codex-cli 0.146.0` 只执行了一次自然结构任务：Router request `019fc7af-142b-7c72-8778-69ef315de22d` 仅一次 Grok attempt、HTTP 200、无 fallback；Sub2API request `7f12f121-fee9-4930-86a6-66057f615abb` 命中 group 5、provider Grok、账号 `101161` 并返回 200。

这次未对 311 个账号逐号生成。全量结论来自结构、调度、被动 refresh 与代理池证据；真实模型调用覆盖只是一条分组链路。

## 11. 逐槽补洞与定时维护终态

2026-08-03 23:17:55 至 23:20:55（Asia/Shanghai）的正式运行验证了增量策略：400 个固定 bridge 槽中，298 个健康槽保持原端口，87 个坏槽被当期新候选替换，15 个暂时没有替代的坏槽保留原端口并由 Resin 熔断隔离；发布代共有 385 个健康槽和 385 个唯一出口。没有因为缺少 15 个替代而放弃已经找到的 87 个补洞，也没有压缩端口导致后续逻辑账号出口整体位移。

常驻 bridge 重启复验后，Resin 同步输入 400、双目标通过 363、选择 320、唯一出口 362、写入节点 365，任务成功提交新代。同步完成瞬间 Platform 可路由 319；稍后的全局只读快照为 371 个节点、335 个 healthy、332 个唯一健康出口，GrokEU 可路由 331、出口 330。两组数字采样时刻不同，不能混写成同一原子快照。

23:36:16 的 Sub2API 只读短事务显示：311 个 live Grok OAuth 全部 `active + schedulable`，311 个 `proxy_id` 和 311 个 `GrokEU.shard-N` 逻辑身份均唯一，永久 error、overload、非 429 temp 均为 0；当时 38 个账号处于正常 429 冷却，立即可选 273 个。429 数量会随真实流量和 reset 窗口变化，必须连同采样时间判断，不能把本文任何历史数值当作永久库存。

第一次同步在 Admin API 写入前的 SQLite 在线备份阶段报 `PermissionError`。根因是 hardened unit 清空 capability，而 Resin DB/WAL/SHM 由 `resin-grok` 以 `0600` 创建；受限 root 无权读取。修复保留了空 capability，只给 UID 0 精确的目录遍历、文件只读与新 sidecar 默认 ACL。随后 `state.db`、`cache.db` 在线备份和 `integrity_check` 均成功，Resin 仍独占写权限。原 ACL、原 unit、原生产配置、原 runtime config 和完整性通过的 `state.db` 备份保存在 `private/runs/pool-cadence-20260803T144036Z/`；主备份 SHA-256 为 `a16613e040048890b72e2991def6c8f5895f242d1b7a21026f6a15997633f372`。

最终调度分层为：Resin 每小时主动探测节点到 `auth.x.ai` 公共 OIDC 端点；GitHub bridge 每 3 小时复测全池并只补坏槽；主链失败时才启用论坛 fallback，论坛发现最多每 24 小时一次。400 是备用容量目标，320 是发布健康底线，不再把一次找齐几百个新节点作为维护前提。
