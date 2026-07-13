# Grok Sub2API 对账、导入与清理契约

## 目标

1. 确认每个候选 auth 是否对应 Sub2API 中的唯一 Grok 账号。
2. 用指定账号 test 区分正常、额度耗尽、revoked、权限错误和不确定故障。
3. 补齐确认可用账号的 Grok 分组绑定与调度状态。
4. 对不可恢复账号生成逐 ID 清理候选；先恢复点、后授权、再执行。

## 运行时配置发现

不得写死端口、Grok 组 ID、容器名或密钥路径。先执行只读发现：

```bash
cd /root/grok-build-auth
systemctl show grok-register-bridge \
  -p FragmentPath -p EnvironmentFiles -p ExecStart --no-pager
systemctl show v2ray-grok-pool \
  -p FragmentPath -p EnvironmentFiles -p ExecStart --no-pager
```

从发现的 env 文件读取以下变量，但不回显密钥内容：

```text
SUB2API_BASE / SUB2API_URL
SUB2API_GROK_GROUP_ID / SUB2API_GROUP
SUB2API_POSTGRES_CONTAINER / SUB2API_PG_USER / SUB2API_PG_DB
SUB2API_ADMIN_KEY_FILE / BRIDGE_MANAGEMENT_KEY_FILE
BRIDGE_PORT
```

再通过 Sub2API 管理元数据或数据库核对：目标 group ID 的 name 为 `grok`，目标账号 platform 为 `grok`。配置缺失或指向不一致时停止写入。

## 两条注册路径

- 服务器协议：`scripts/register_and_import.py` 直接完成 preprobe、备份、导入和 postprobe。
- 外部 Windows：客户端完成注册、OAuth 和客户端 preprobe，再把 Sub2API auth 推送到 bridge。

两者并列保留，服务器不运行 Windows 客户端。废弃的是旧 quarantine 流程：先把无组/不可调度账号写入数据库，再依赖后续 probe 筛选。

## Auth 收集与去重

1. 只读取用户指定目录和批次 manifest 明确列出的 `xai-*.json`。
2. 校验 access token、refresh token、email/sub 和官方 CLI base URL；不打印值。
3. 按稳定账号身份、email 和 token hash 与 Sub2API 对账；不能只按文件名。
4. 同一身份出现多个 auth 时保留来源、mtime 和 hash 证据，由 probe 与数据库更新时间决定候选，不直接覆盖。
5. `cpa_*` 和 `cpa_auths/` 是客户端兼容名称；业务产物称为 Sub2API auth，schema 保持 CLIProxyAPI-compatible。

## Bridge 契约

客户端同时检查 HTTP 状态、`probe`、`error_code`、`imported`、`action` 和 `account_id`：

| 阶段 | 期望行为 | 核验 |
|---|---|---|
| 写库前 preprobe 失败 | 422，零 create/update | `imported=false` 且数据库无变化 |
| create 后 postimport 失败 | 精确 ID 回滚 | 响应说明回滚结果，数据库无活动残留 |
| update 后 postimport 失败 | 恢复旧凭据、分组和调度 | 原账号仍保持旧可用状态 |
| 全部通过 | 200，passed/imported | group、schedulable、指定账号 test 均正确 |

不能把所有 422 一概描述成零写入。遇到 postimport error 或超时，先按账号 ID 和 token hash 对账，再决定是否幂等重推。

## 全量账号验证

先导出所有未删除 Grok 账号的精确 ID、脱敏名称、分组和调度状态，再逐 ID 调用 Sub2API admin account test。保留原始状态码、结构化错误类别和时间，不保存 token 或完整敏感响应。

| 分类 | 证据 | 处置 |
|---|---|---|
| `ok` | completed 且输出匹配 | 保留，确保 Grok 分组和 schedulable |
| `quota` | 明确 402/429 spending/free-usage | 保留；仍算可用，等待 reset/cooldown |
| `revoked` | invalid_grant/refresh revoked | 优先 remint 更新原账号；不可恢复才列候选 |
| `permission` | 重复指定账号 test 均为 entitlement/TOS | 等传播窗口后复测；持续失败才列候选 |
| `transient` | TLS、代理、超时、5xx | 不删除，修复链路后复测 |
| `ungrouped` | 无 Grok 分组 | 查 manifest、bridge action 和历史 quarantine 证据 |

分组 Responses 可验证整体入口，但不能替代逐账号 test。一次失败不足以证明账号不可用，尤其不能把 402/429 当坏号。

## 补齐与导入

1. 候选 auth 先通过客户端或服务器 `/responses` preprobe。
2. 查询稳定身份是否已存在；存在则 update，不创建重复账号。
3. 外部客户端通过 bridge；服务器协议通过正式导入器，不直接拼 SQL 写凭据。
4. 成功后核对唯一 Grok 分组、schedulable、官方 base URL 和指定账号 test。
5. auth 已入库不等于文件可立即删除；先记录 auth hash 到账号 ID 的映射和恢复点。

## 清理流程

清理分三类独立授权：Sub2API 账号、服务器/客户端 auth 文件、Mailu 邮箱。

1. 建立并验证数据库恢复点。
2. 生成不可变候选清单：账号 ID、脱敏身份、分类、至少一次决定性证据、恢复能力、对应 auth 精确路径。
3. 排除 quota、transient、待传播 permission、归属不明和仍可 remint 的账号。
4. 取得逐 ID 或逐文件授权；宽泛的“清 Grok”不能替代精确清单。
5. 使用正式管理 API 或项目恢复工具处理，不直接执行无 where 保护的 SQL。
6. 执行后重新导出账号、分组和 auth 映射，确认只影响批准目标。

旧 quarantine 残留也必须逐 ID 证明：无组本身不是删除证据。需要结合创建时间、bridge/manifest 来源、指定账号 test 和恢复可能性。

## 完成证据

- 配置发现记录，不含秘密。
- 数据库恢复点及可读性验证。
- 全量账号分类统计和逐 ID 结果。
- 可用 auth 到 Sub2API 账号的映射差集为零。
- 正常与 quota 账号均保留；清理目标均有授权和决定性证据。
- 清理后指定账号抽检、分组 `/v1/responses`、Git 状态和提交推送结果。
