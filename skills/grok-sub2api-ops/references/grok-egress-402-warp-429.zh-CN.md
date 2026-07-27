# Grok 402 出口恢复、WARP 验证与 429 预绑定

## 目录

1. 目标与结论
2. 出口拓扑与代理语义
3. 402、429 与传输故障的分流
4. 2026-07-27 事件时间线
5. 402 恢复流程
6. 429 无探针预绑定流程
7. 随机与均衡的准确含义
8. 备份、回滚与批次证据
9. 运行后验收与持续监控
10. 公开资料与证据边界

## 目标与结论

本流程解决的是 Grok/xAI OAuth 账号因出口路径异常出现批量 `402`，以及真实 `429` 账号在额度窗口结束后仍需继续使用已验证出口的问题。它不把真实额度耗尽伪装成已恢复，也不通过重复模型请求撞额度状态。

关键结论：

- 同一批账号需要的是稳定、已验证且按账号粘性的出口，不是每次请求随机换 IP。
- Cloudflare WARP 只是一种候选出口技术，不能把“换出口后恢复”写成“WARP 必然修复 spending-limit”。
- 当前生产恢复池是三个已有账号级恢复证据的区域出口；WARP SJC 只有链路健康 canary 证据，不进入账号批量绑定池。
- 疑似出口型 `402` 可以用单账号、单次指定账号探针验证；成功后再分波绑定。
- 已有被动证据的真实 `429` 只做出口预绑定，禁止 `/test`、生成请求和 cooldown 清理。完成状态是 `binding_applied`，不是 `quota_recovered`。
- 真正的 `free-usage-exhausted`、rolling 24-hour 或 included free usage 用尽不会因换 IP 恢复，应保留 reset/cooldown 等待自然到期。

## 出口拓扑与代理语义

2026-07-27 的生产映射如下。每次操作前仍须通过 Admin API 和服务状态重新核验，不能只信这张表。

| Proxy ID | 名称/区域 | 本机入口 | 用途 |
|---|---|---|---|
| 9 | Cloudflare WARP US/SJC | `172.17.0.1:10828` | 仅链路 canary 与出口研究，尚无账号级恢复证据 |
| 10 | JP/KIX verified relay | `172.17.0.1:10830` | 生产账号粘性出口 |
| 11 | TW/TPE verified relay | `172.17.0.1:10831` | 生产账号粘性出口 |
| 12 | FR/CDG verified relay | `172.17.0.1:10832` | 生产账号粘性出口 |

对应服务：

- `grok-warp-egress.service`
- `grok-jp-egress-relay.service`
- `grok-tw-egress-relay.service`
- `grok-fr-egress-relay.service`

`proxy_id` 是账号的持久绑定。所谓“随机分配”是计划阶段随机决定某个账号归属 10/11/12 中哪一个，同时保持三个出口总负载均衡；完成后同一账号继续走固定出口。不要为每次请求重新随机代理，否则会增加身份风控、地域漂移和故障定位难度。

## 402、429 与传输故障的分流

先消费最近的被动 usage snapshot、cooldown 和历史指定账号证据，再决定是否探针。不得用全量 Test Connection 生成候选。

| 证据 | 分类 | 动作 |
|---|---|---|
| snapshot `402`，账号凭据/分组正常，换已验证出口后单次 test completed | 出口型假 `402` | 保留新出口，清理探针产生且已过期的临时状态，恢复调度 |
| `free-usage-exhausted`、`rolling 24-hour`、`included free usage`，通常为 `429` | 真实额度耗尽 | 保留账号和 reset/cooldown；若尚无出口，仅做无探针预绑定 |
| `cli-chat-proxy` EOF、TLS、超时、普通 `5xx` | 传输不确定 | 换已验证出口做有上限的单次重试，不删除账号 |
| test 报错但随后 snapshot 已变 `200` | 状态机竞态/歧义 | 标记 ambiguous，人工核对；不得自动连续 test |
| `invalid_grant` / refresh revoked | OAuth 链失效 | 进入 revoked 快路径，不套用出口恢复 |
| permission/TOS | 资格或策略问题 | 等待传播或人工处置，不因换出口直接判恢复 |

状态码本身不够。`402` 可能是出口、接口或请求形态问题；`429` 可能是明确额度，也可能是普通速率限制。必须结合错误语义、reset 时间、请求目标是否为 `cli-chat-proxy.grok.com`、账号历史和换出口前后对照。

## 2026-07-27 事件时间线

### 基础设施

- `19:59`：生成 WARP 注册与 WireGuard profile。
- `20:00-20:01`：`grok-warp-egress.service` 因 `address family not supported by protocol` 反复启动失败。
- `20:02`：wireproxy 可启动并解析 xAI/Grok 域名。
- `20:16`：配置加入 `ResolveStrategy = ipv4`，WARP canary 出口稳定运行。
- `20:25`：JP/KIX relay 上线。
- `20:31`：TW/TPE relay 上线。
- `20:42`：FR/CDG relay 上线。

### 402 恢复批次

恢复 helper 位于 `/usr/local/libexec/grok-egress-recovery/recover_batch.py`，批次证据位于 `/var/lib/grok-warp-egress/recovery-*`。它会隔离账号、绑定出口、执行指定账号 test、恢复调度并 GET 验收；不适合 429 无探针绑定。

进入 402 处理链的账号共 264 个：单独 canary 1 个，加批处理唯一账号 263 个。批处理按 `9 + 30 + 60 + 82 + 30 + 30 + 22` 分波，主要轮询 10/11/12。

批处理唯一账号的最终即时分类：

| 分类 | 数量 | 说明 |
|---|---:|---|
| recovered | 260 | 指定账号 test completed，终态 snapshot `200` |
| quota_exhausted | 2 | 明确 rolling 24-hour free usage 耗尽 |
| failed | 1 | 同类额度错误在早期波次被记为 failed |

另有两次传输/竞态失败在单账号重试后恢复。单独 canary 账号 `100904` 最终使用的是 proxy 10 / JP-KIX；没有发现任何账号绑定 proxy 9 后执行恢复 test 的证据。该结果证明“已验证区域出口能恢复这一批出口型 402”，不能证明 WARP 品牌本身是充分条件，也不能反向证明 proxy 9 对账号恢复必然无效。

### 后续只读快照

在本轮 429 预绑定前，Grok 分组共有 313 个账号：

- usage snapshot：`200=258`、`402=0`、`429=55`
- 已绑定：proxy 10 为 91、proxy 11 为 90、proxy 12 为 83
- 未绑定：49，全部为 active、schedulable、Grok 分组内的真实 `429`
- 已绑定但仍为 `429`：6

这说明换出口消除了当时的批量 `402`，但部分账号随后因真实使用量进入 `429`。两类问题必须分开报告。

### 本轮 429 预绑定收口

`2026-07-27 22:42 +08:00` 重新生成 plan 时，旧审计的 49 个未绑定 `429` 已发生实时漂移：账号 `101084` 的额度窗口在约 `22:27` 到期，随后从 `429` 变为新的未绑定 `402`，reset 又推进到次日。这是“429 到期后若仍走旧出口，可能重新出现出口型 402”的现场证据。

因此本轮分为两条互斥路径：

- 48 个仍为真实 `429` 的账号：plan 配额 `10:+13、11:+14、12:+21`，全部无探针绑定成功，snapshot/cooldown 保持不变。
- `101084`：绑定 proxy 10，执行唯一一次指定账号 test，得到 `test_complete`，终态恢复为 `200`。

最终只读验收：

- Grok 分组账号 `313`，唯一 ID `313`
- `active=313`，`schedulable=true=313`
- usage snapshot：`200=256`、`402=0`、`429=57`
- proxy 分布：`10=105`、`11=104`、`12=104`
- 未绑定 `0`，proxy 9 绑定 `0`，错误分组 `0`
- 48 个 429 绑定失败 `0`；本轮账号 `/test` 仅 `101084` 一次

批次证据位于 `/root/grok-build-auth/private/runs/20260727T144256Z-grok-429-egress/`。plan SHA256 为 `8dc0adbd85e74966c805f43cae20d5581f892a4e8affe83780634bcdb6129e86`；写入前 custom-format 恢复点为 `49,299,758` bytes，SHA256 `bfd509e1f7698638b55b1e49a4ded9b0a58fc8d9d238dae2f2d2feefebfcb38c`，并再次通过 `pg_restore -l`。

## 402 恢复流程

只在对象明确为 Grok OAuth 账号、用户授权生产写入且已有恢复点时执行。

1. 从被动 snapshot 冻结 `402 + active + schedulable + 目标 Grok 分组 + 非 child + proxy 空` 的候选集。
2. 通过 Admin API 核验出口 proxy 为 active，并从主机服务和历史证据确认它属于已验证生产出口；不要靠名称猜测。
3. 先选一个账号做 canary：保存 before，临时 `schedulable=false`，绑定一个已验证区域出口，只执行一次指定账号 Responses test。
4. test completed 且终态 `200` 时恢复调度并保留绑定；明确额度耗尽时保留绑定和 cooldown；传输失败只做有上限的换出口重试。
5. canary 通过后分小波扩展，设置失败阈值；每个账号单独快照和回滚。
6. test 可能更新 snapshot/cooldown，而 Admin API 不能精确写回这些字段。失败回滚只能保证 proxy 和 schedulable，不得宣称完整撤销上游状态副作用。
7. 最终按唯一账号而不是日志行数汇总 recovered、quota_exhausted、ambiguous 和 failed。

不要把 host helper 的 `--allowed-snapshot-statuses` 改为 `429` 后直接复用。该 helper 会无条件调用 `/test`，与真实 429 的无探针要求冲突。

## 429 无探针预绑定流程

使用本技能 `scripts/bind_quota_egress.py`，采用 `plan -> apply` 两阶段。

### Plan

1. 分页读取目标 Grok 分组和指定 proxy pool。
2. 重新冻结 `snapshot=429 + rate_limit_reset_at 非空 + active + schedulable + 目标分组 + 非 child + proxy 空` 的账号。
3. 记录每个账号的非秘密 before 字段、当前 proxy 总量、随机种子、均衡配额和账号到 proxy 的映射。
4. 生成 canonical plan、SHA256 和 `0700/0600` 批次证据。账号列表不能从旧会话摘要直接复用。
5. 计划阶段不得调用 `/test`、模型接口、DELETE cooldown 或任何账号写接口。

### Apply

1. 要求显式 `--confirm-production-write`，校验 plan hash，并重新 GET 全部账号和 proxy 状态。
2. 写入前创建一次 PostgreSQL custom-format dump，使用 `pg_restore -l` 验证并记录 SHA256。
3. 对每个账号再次 GET；临时隔离调度；仅 PUT 指定 `proxy_id`；恢复原 schedulable；GET 验收。
4. 不调用 `/test`，不生成请求，不清 `rate_limit_reset_at`、temp cooldown、overload 或 usage snapshot。
5. 单账号失败只回滚该账号的 proxy 和 schedulable；原 proxy 为空时用 `proxy_id: 0` 清绑定。
6. 中断后再次 apply 时，已经绑定到计划 proxy 且其他约束正确的账号记为 `already_applied`，不得重复写或探针。
7. 结果写为 `binding_applied` 或 `already_applied`。即使 GET 仍为 `429` 也属于预期；不要写成 recovered。

## 随机与均衡的准确含义

计划算法先计算三个出口在绑定全部候选后的近似相等目标，再随机打乱候选账号，并按配额分配。以预绑定前 `10=91、11=90、12=83` 和最初审计的 49 个未绑定额度账号为例，最终均衡结果是：

- proxy 10 新增 14，最终 105，其中 48-account plan 新增 13，漂移成 402 的 `101084` 单独新增 1
- proxy 11 新增 14，最终 104
- proxy 12 新增 21，最终 104

具体账号到出口的映射由 plan 中的 seed 决定并由 SHA256 锁定。apply 不重新随机，避免计划和实际不一致。

## 备份、回滚与批次证据

批次统一放在项目 `private/runs/<batch-id>/`：

```text
plan.json
plan.sha256
backup/*.dump
backup.json
apply-results.jsonl
apply-summary.json
```

- 目录权限必须为 `0700`，文件为 `0600`。
- dump 使用 `pg_dump -Fc --no-owner --no-acl`，随后以 `pg_restore -l` 验证。
- 记录文件大小、SHA256、创建时间、数据库容器/库/用户的非秘密标识。
- 不记录 admin key、token、密码、邮箱、代理凭据或真实公网出口 IP。
- 数据库恢复点用于灾难恢复；正常错误优先按账号 PUT 回滚，避免为了一个账号回滚整批数据库。
- 保留本批恢复点，直到用户的明确备份保留策略允许清理。

## 运行后验收与持续监控

一次完整收口至少验证：

- 目标 Grok 分组账号总数与操作前一致。
- 所有账号均为 `active`，预期账号保持 `schedulable=true`。
- 目标账号的 `proxy_id` 全部属于锁定的生产 pool，未绑定数为 0。
- `402=0`；`429` 可大于 0，且 reset/cooldown 未被本次绑定脚本清除。
- proxy 9 未被批量绑定。
- JP/TW/FR relay 和 Sub2API proxy 均 active。
- plan、backup、results 和 final summary 的 hash、权限及数量相互一致。

出口是新的共享依赖。任一 relay 长期故障会影响粘在其上的一组账号，因此应监控 service active、SOCKS listener、Sub2API proxy 状态和被动 402/5xx 增量。监控只用 health/status 和近期被动结果；不要把模型生成请求做成高频心跳。

## 公开资料与证据边界

LINUX DO 与 GitHub 的公开证据支持“某些裸 402/spending-limit 与出口 IP 或服务器路径相关，换出口后可恢复”，但没有找到严格的“同一 spending-limit 账号使用 WARP 前后对照”公开硬证据：

- https://linux.do/t/topic/2658684/3
- https://linux.do/t/topic/2658684/4
- https://linux.do/t/topic/2658684/6
- https://linux.do/t/topic/2660400/1
- https://linux.do/t/topic/2660400/3
- https://linux.do/t/topic/2660400/8
- https://linux.do/t/topic/2563241/2
- https://linux.do/t/topic/2632643/1
- https://github.com/ywddd/grok-inspection/issues/27

因此对外和在技能内统一使用以下表述：

- 事实：本机历史 402 与随后 10/11/12 的账号级批次结果证明，已验证区域出口能恢复这一批出口型 402。
- 推断：IP 信誉、地区、出口 ASN、链路或接口形态可能参与触发。
- 未证实：WARP 本身必然恢复 `personal-team-blocked:spending-limit`，或 proxy 9 已完成同账号恢复 A/B。

不要将论坛经验替代本机 canary，也不要把真实 429 的自然到期归因于换 IP。
