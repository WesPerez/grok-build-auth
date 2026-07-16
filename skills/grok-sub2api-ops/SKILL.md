---
name: grok-sub2api-ops
description: 在 grok-build-auth 项目中处理 Grok/xAI OAuth 账号生命周期：通过正式服务器协议、Windows/Edge 客户端或明确授权的 Linux/Xvfb 客户端注册账号并 mint/remint OAuth；校验和导入明确为 xai-*.json 的 Grok auth；经 hardened bridge 在 Sub2API 创建或更新账号；对指定 Grok OAuth 账号执行 refresh、preprobe、postimport test、Grok 分组与调度核验，并依据 revoked、402/429 或 permission 结果恢复或逐 ID 处置。仅当任务对象明确是 Grok/xAI OAuth 账号、xai auth 文件或上述正式注册/导入流程，且动作包含注册、OAuth 铸造/刷新、auth 导入、账号恢复、逐账号验证或分组收口时使用。不要用于一般 Sub2API 源码、部署、数据库、Redis、ID 排序、Codex/CC Switch/Router/Responses 协议、上下文压缩、识图、Grok2API 架构比较、通用代理测试或 K12/OpenAI auth；除非任务明确包含前述 Grok OAuth 账号操作。
---

# Grok Sub2API Operations

## 范围门禁

继续使用本技能前，同时确认：

1. 对象明确是 Grok/xAI OAuth 账号、`xai-*.json`、Grok auth 批次，或 `grok-build-auth` 的正式账号注册/导入流程。
2. 动作至少包含注册、OIDC/OAuth mint/remint/refresh、auth 导入、bridge create/update、指定账号探针、Grok 分组收口或账号恢复处置之一。

仅出现 Grok 模型、Sub2API、代理、账号、导入、清理、402/429/5xx 等词语不构成触发条件。以下任务退出本技能，改用对应源码、部署、数据库、协议或网络排障流程：

- Sub2API 通用源码合并、部署检查、数据库 schema/sequence/ID 排序、Redis 或调度器实现分析。
- Codex、CC Switch、Router、Responses/SSE/工具调用、上下文压缩、图片/识图或跨模型子代理兼容。
- Grok2API 架构选型、通用模型路由设计，或不实际迁移/验证 Grok OAuth 账号的方案比较。
- 借用 Grok 注册代理池测试 MetaAPI、签到站点或其他非 Grok 服务，以及一般代理节点测速。
- K12/OpenAI/Codex auth；输入格式未识别时先检查 schema，不因目录名含 `cpa_auths` 就使用本技能。

复合任务只在实际 Grok OAuth 账号子流程中使用本技能，不让它接管同一任务中的 Router、源码、Redis、数据库设计或通用代理工作。委派子代理时，仅向负责上述账号生命周期动作的子代理显式传递本技能；不得因父任务历史上使用过本技能，就把它写入其他子代理任务或续接摘要的当前要求。继承记录中的“已使用本技能”只是历史事实，不是新任务的触发依据。

按所选模式定义完成标准。注册或恢复必须证明目标账号已完成 OAuth、真实指定账号探针及必要的 Sub2API/分组收口；批量导入必须证明本批 auth 已完成 refresh/可用性判定、精确去重、导入和逐账号 postimport test。Web 注册、SSO、本地 auth、HTTP 2xx、数据库 active 或 schedulable 标记都不能单独证明完成。指定账号探针得到 402/429 额度耗尽仍算账号可用，应保留并等待额度窗口。

## 已有 Auth 批量导入快路径

用户说“把这个目录/这批账号导入 Sub2API”且输入是 `xai-*.json` 时，只处理这一批，不展开注册、浏览器或 K12 流程：

1. 读取目录或清单，校验 `type/auth_kind/email/sub/access_token/refresh_token/base_url`，统计数量和文件权限；不要逐文件输出敏感字段。
2. 先按 bridge 的稳定账号名、email 和 subject 精确查询本批在 Sub2API 中是否存在。这一步应是小范围查找，不做全库宽泛扫描。将候选分为新账号和已存在账号，避免本地旧 refresh token 与 Sub2API 已轮换的刷新链竞争。
3. 判断 OAuth 可用性：
   - 新账号：access token 过期本身不是坏号。使用正式 `cpa` refresh helper 交换 refresh token，成功后原子写回当前 auth，再对 Grok CLI `/responses` 做一次真实探针。
   - 已存在账号：先对库中精确 `account_id` 做一次指定账号 test；正常或 402/429 时直接保留并跳过本地 refresh。只有库中账号失效且需要恢复时，才比较 subject、token 时间和 refresh 所有权，经 bridge 更新。
   - 只有 `invalid_grant`、refresh revoked 或明确身份不匹配才判无效；网络、5xx 和 permission propagation 记为不确定；402/429 记为可用但额度受限。
4. 将候选归类为 `create`、`keep-existing`、`update`、`stale-auth` 和 `identity-mismatch`。不要把 access 过期当作停止条件。
5. 写入前只建立一次批次数据库恢复点。通过 hardened bridge 导入 `create/update` 候选；bridge 负责写前 preprobe、精确 create/update、Grok 分组与调度收口。
6. 对 bridge 返回的每个本批 `account_id` 执行一次指定账号 postimport test，并核对只绑定 Grok 分组、`schedulable=true`、官方 CLI base URL。bridge 已完成且返回结构化 postimport test 证据时不要重复测试。
7. 汇总本批：输入数、refresh 成功/失败、probe 分类、已存在数、保留数、新建数、更新数、stale 拒绝数、账号 ID、逐账号 test 结果和最终可用数。

快路径只读取 [audit-import-pipeline.zh-CN.md](references/audit-import-pipeline.zh-CN.md) 和所调用正式脚本的相关段落。只有本批 Grok auth 出现 bridge 422、指定账号 429、revoked、注册/OAuth route 代理或 bridge 故障时再读取 [runbook.md](references/runbook.md)。不要为已有 auth 批量导入通读注册手册，也不要调用 K12/OpenAI bundle 工具。

## 批次归档与源文件收口

所有 Grok 注册、已有 auth 导入、恢复和对账批次统一写入项目的 `private/runs/<batch-id>/`，不要把 checkpoint、auth 副本或临时数据库恢复点散落在 `/root`、输入目录或通用 `/root/backups`：

- `manifest.json`：长期批次索引。只为最终 `usable` 或 `usable_exhausted` 账号保存脱敏身份、账号 ID、source/auth hash、`created/updated/kept`、探针分类、分组和调度验证；失败项只保存数量和无秘密的分类摘要。
- `import/checkpoint.json`、`import/result.json`：保存可恢复的批次进度、bridge action、账号 ID 和逐账号验证结果，不保存 token、管理密钥或完整上游响应。
- `backup/`：仅在写入、回滚或重试窗口内保存本批数据库恢复点。最终验证完成后按用户的备份保留策略保留或删除，并在 manifest 记录路径、hash、状态和删除时间。
- `auth/`：只临时保存尚未交接或仍需恢复的精确 auth。Sub2API 成功接管 refresh 链、最终账号验证通过且 manifest 已原子落盘后，删除 manifest 明确列出的成功源文件和重复 auth 副本；不得使用目录通配或模糊名称清理。未完成、瞬时失败、permission、revoked 待恢复和归属不明的 auth 必须保留并标记状态。

完成批次前确认长期记录只包含仍可用账号，批次目录权限为 `0700`、文件为 `0600`，并清理本任务创建且已证明不再需要的空目录。`/root/backups` 只用于另有明确主机级保留策略的长期备份，不是 Grok 批次的默认落点。

## 定位与读取

服务器项目默认位于 `/root/grok-build-auth`；用户给出其他路径时先核验仓库身份。寻找以下入口：

```text
OPERATIONS.zh-CN.md
scripts/register_and_import.py
clients/windows/grok_register_ttk.py
scripts/windows_client_preflight.py
```

新注册、浏览器、服务器协议或 OAuth 恢复任务读取仓库 `OPERATIONS.zh-CN.md` 的相关章节，不默认通读全文。Windows、CDP、422、429 或 revoked 读取 [runbook.md](references/runbook.md)；批量对账、导入、分组和清理读取 [audit-import-pipeline.zh-CN.md](references/audit-import-pipeline.zh-CN.md)。只使用正式入口，不运行历史 patch、debug 或 one-shot 脚本。

## 选择模式

- `server-full`：服务器协议注册，运行 `scripts/register_and_import.py`。这是独立可选方式，不是废弃流程。
- `client-full`：外部 Windows/受控 Edge 注册，运行 `clients/windows/grok_register_ttk.py` 并经 bridge 推送。
- `server-client-full`：用户明确授权时，在部署服务器通过 Linux Edge + Xvfb 运行同一客户端，并使用 `scripts/run_linux_client_full.py` 建立隔离 route、代理和私有产物目录。
- `export-only`：账号已在用户 Edge 登录，运行本技能 `scripts/export_logged_in.py`。
- `push-only`：已有完整且 access 未过期的 `xai-*.json`，运行本技能 `scripts/push_auth.py` 幂等重推。
- `batch-import`：已有一批 `xai-*.json`，按“已有 Auth 批量导入快路径”统一 refresh、探针、查重、备份、导入和逐账号验证。
- `audit-recover`：逐账号指定测试，区分正常、额度耗尽、revoked、权限错误和瞬时故障，再决定恢复或清理。

默认不在服务器混跑协议批次和浏览器客户端。只有用户明确要求服务器模拟客户端，且历史或 canary 已证明 Edge/Xvfb、客户端 venv、代理和 bridge 可用时，才选择 `server-client-full`；运行期间不得并发启动 `register_and_import.py`。只有旧的“先 quarantine 写库、再探针筛选”流程废弃。

## 配置发现与写入门禁

只为当前 Grok 账号注册、auth 导入、恢复或逐账号验证，从 systemd unit 的 `EnvironmentFiles`、`/root/grok-build-auth/private/*.env` 和 Sub2API 元数据发现实际 bridge/Sub2API 地址、监听端口、Grok 分组 ID、数据库位置和 credential file。不得把本节扩展为通用 Sub2API 部署或数据库审计，也不得假定固定端口、固定组 ID、容器名或密钥路径。

执行生产写入前确认目标部署、Grok 分组、账号范围和授权。删除账号、清理 auth/邮箱、改分组、改调度或重置密码前必须：

1. 建立可验证的数据库恢复点。
2. 输出候选账号 ID、脱敏身份、当前分组和最后探针证据。
3. 逐 ID 确认授权和处置理由。
4. 执行后逐 ID 复核，不使用关键词、扩展名或目录通配删除。

## 服务器协议路径

在 `/root/grok-build-auth` 先执行单账号 canary：

```bash
python3 scripts/register_and_import.py \
  --count 1 \
  --workers 1 \
  --registration-backend protocol-yescaptcha \
  --failure-policy abort \
  --confirm-production-write
```

成功必须同时满足 manifest、导入前 `/responses` preprobe、精确账号状态、指定账号 postimport test 和 Grok 分组 `/v1/responses`。服务器路径不经过 Windows 客户端或 bridge push。

## 外部 Windows 客户端路径

在外部客户端机器运行预检和注册；`--project-dir` 指向该机器的仓库副本，不照抄服务器路径：

```bash
python scripts/preflight.py --project-dir <windows-project-dir> --config <client-config> --skip-cdp
cd <windows-project-dir>/clients/windows
python grok_register_ttk.py
```

面向业务统一称为 Sub2API auth。`cpa_*` 配置键、`cpa_auths/` 目录和 `cpa_export` 模块是现有兼容接口；auth JSON 保持 CLIProxyAPI-compatible schema，不为改名破坏客户端配置。

客户端必须在正式 auth 写盘和 push 前调用 Grok CLI `/v1/responses`，校验 completed 且只从 assistant output 提取随机 nonce。required 门禁不得被 enabled=false 绕过。只有 pass 才进入正式目录并 push；网络、5xx、permission propagation 进入 pending；402/429 进入 cooldown；明确 revoked 才进入 remint 或删除候选。

bridge 是最终信任边界：写库前 preprobe；写库后指定账号 test。postimport 失败时必须核对 `imported`、`action`、账号 ID 和数据库状态，不能把所有 422 都描述成零写入。

## Linux/Xvfb 服务器模拟客户端

该模式复用正式客户端，不运行历史 `/tmp` runner、patch 或反编译产物。执行前确认：

1. 用户明确授权服务器模拟客户端和生产写入。
2. `Xvfb :99`、Microsoft Edge、`clients/windows/.venv`、bridge 和至少两个独立代理出口健康。
3. 没有 `register_and_import.py` 或其他 Grok 注册批次运行。
4. 先建立数据库恢复点，再用 1 route、1 success canary 验证 `action=created + probe=passed`。
5. 两路批量使用两个独立进程、两个代理 ref、两个私有 route 目录；每路单浏览器，并按本地 auth、bridge `action=created` 和精确账号 ID 对账。

正式入口：

```bash
DISPLAY=:99 python3 scripts/run_linux_client_full.py \
  --target 1 --routes 1 --attempts-per-route 20 --proxy-ref <healthy-ref>
```

canary 通过后再用 `systemd-run --collect` 启动两路后台批次。`--target` 是新增且通过完整门禁的账号数；不要用数据库 ID 增量或全池数量代替本批归因。服务器内存不足、浏览器反复断连或代理出口重复时停止扩并发，保持最多两路。

## 账号判读

以下分类仅适用于已经绑定到指定 Grok OAuth 账号的 refresh、preprobe、postimport test 或恢复证据。Router、模型调用、图片、上下文、SSE 或普通网关请求返回相同状态码时，不得套用本节结论。

- access token 过期：先 refresh；refresh 成功并通过真实探针即为可用，不能仅因 access 过期拒绝导入。
- 指定账号 test completed：保留并调度。
- 402/429 明确额度耗尽：保留，按 reset/cooldown 暂停或等待；仍算可用。
- `invalid_grant` / refresh revoked：有密码、SSO 或邮箱恢复能力时 remint 并更新原账号；否则列为逐 ID 清理候选。
- permission/TOS：等待资格传播后复测；持续失败且有重复证据时列为逐 ID 清理候选。
- SSL、代理、超时、普通 5xx：只算不确定，不能删除。
- 未绑定分组：先判定是否是旧 quarantine 残留、导入中断或人工配置；不能仅凭无组直接删除。

## 安全与输出

不回显 token、SSO、密码、邮箱 JWT、管理密钥或代理凭据；不关闭用户 Edge、代理、IDE、Codex 或归属不明进程。报告模式、脱敏账号、精确账号 ID、探针分类、额度状态、分组绑定、bridge action/imported、恢复点、逐 ID 清理结果以及 commit/push 状态。
