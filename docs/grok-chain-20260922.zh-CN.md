# Grok 4.7 链路升级与验收记录

验收日期：2026-09-22，Asia/Shanghai。以下数量为本次检查时的快照。

## 结果与范围

Grok2API 已同步检查时的最新上游，并部署 Grok 4.7 支持。实际链路为：

`官方 Codex CLI → Router :13083 → Sub2API :13080 / 账号 2221 → Nginx → Grok2API :18000 → 各账号 Resin 身份 → Grok Build`

普通回复、流式推理、工具调用及工具结果回传、图片输入均已真实通过。
最终镜像部署后又完成了一次官方 Codex CLI 调用，Sub2API 用量记录确认命中账号 2221，
请求模型和上游模型均为 `grok-4.7`。

账号池共 309 个。294 个有效账号完成全量凭据与账单刷新核验，可参与调度；
15 个账号的刷新令牌已被上游撤销，仍需取得新的授权。生成能力通过链路抽样验证，
没有把账单查询成功描述为每个账号都完成过独立生成测试。

## 交付版本与 CI

| 项目 | 交付提交 | 验证 |
| --- | --- | --- |
| Grok2API | `fda7b31fa892bfa150b95e60a0f28fcaf177354a` | [镜像 CI 成功](https://github.com/WesPerez/grok2api/actions/runs/35644401194) |
| Router 与 Catalog | `09e1544d2732261115c25ddb8c85804fbf918699` | [发布 CI 成功](https://github.com/WesPerez/codex-unified-router/actions/runs/35629894795) |
| 账号接入、探针与重新授权工具 | `cae8ed986bf0db05821387b26e648baa7b382f6c` | [main CI 成功](https://github.com/WesPerez/grok-build-auth/actions/runs/35648601086)，144 个测试通过 |
| Resin guard 的平台过滤修复 | `2a082ad7eec07c69f05120e3b748a5ba39b5d48b` | [Guard CI 成功](https://github.com/WesPerez/server-scheduled-tasks/actions/runs/35629276893)、[调度台 CI 成功](https://github.com/WesPerez/server-scheduled-tasks/actions/runs/35629276782) |

以上提交均已进入对应远端 main。Grok2API 上游基线为
`chenyme/grok2api@5e5ad75556b61a2c4a8fcf344d83bfe7760f2b42`，包含 Build 1.0.40 协议更新。
部署镜像固定为：

```text
ghcr.io/wesperez/grok2api@sha256:d87c9b7e2a2feaefb800a1d02c6b3a79a1b4771d9e543a13789676fac6c68128
```

容器镜像的 OCI revision 与交付提交一致，状态 running / healthy，RestartCount 为 0。
`/healthz` 与 `/readyz` 均返回 HTTP 200；Build、模型路由、账单与存储组件 ready。
全局 readiness 的 `degraded` 来自未配置账号的 Web/Console provider，不代表 Build 路径不可用。

## 模型、Catalog 与策略

- Sub2API 账号 `2221 / !B0-grok2api` 的映射为 `{"grok-4.7":"grok-4.7"}`，active 且 schedulable。
  旧质量重试请求头及其覆写开关已移除；最终读回没有错误、限流、过载或临时禁调度标记。
- Catalog 的 overlay、生成文件和 source 元数据均已更新为 Grok 4.7，并同步至 Linux 的
  `/root/.codex/models-router.json` 与 `/root/.codex/models-router-source.json`。
  元数据包含 500000 上下文、图片输入和 `low / medium / high / xhigh` 四档推理。
  新启动的官方 CLI 已使用该 Catalog 完成验收；本次没有做极限上下文容量测试。
- Router 当前 Grok 阶段为 `id=grok`、启用、`match_type=exact`、`pattern=grok-4.7`、`selection=first`。
  原“最新匹配”是在一组模糊候选中自动选较新版本，会在模型目录变化时改变实际选择。
  现在由配置明确指定完整模型名，版本升级时显式改名。
- `prefix/glob` 匹配、最高版本候选选择函数及 UI 下拉已移除。
  旧 exact 配置的 `selection=latest` 仅在读取时归一为 `first`；这属于迁移兼容，不再执行择新。
- 显式路由顺序、账号调度、额度封锁、失败冷却及原生质量保护仍有独立价值，予以保留。
  原生 `qualityGuard.requestRetry` 与已退役的外置重试代理是不同实现。

Catalog 在磁盘与新启动 CLI 上已验证。已有常驻 App Server 的模型列表未逐实例强制刷新；
若客户端仍显示 4.6，应在其承载任务结束后执行 `rcodex` 再连接，避免中断正在运行的任务。
仓库中的 Windows Catalog 分发文件已更新，本次未声称已经改写另一台 Windows 电脑的本地副本。

## 真实请求验收

| 用例 | 结果 | 证据摘要 |
| --- | --- | --- |
| 非流式文本 | HTTP 200 / completed | `grok-4.7`，19 + 23 返回 `42`，3.23 秒 |
| 流式工具调用 | HTTP 200 / completed | 产生推理事件与唯一 `chain_add` 调用，参数为 19、23 |
| 工具结果回传 | HTTP 200 / completed | 接收对应 call ID 的结果后返回 `42`，1.01 秒 |
| 原生图片输入 | HTTP 200 / completed | 正确识别合成图片左红、右蓝，17.45 秒 |
| 官方 Codex CLI | exit 0 | 结构化结果通过，completed_turns=1、failed_turns=0 |
| 最终镜像后的官方 CLI | exit 0，19.57 秒 | 03:55:22 开始，Sub2API 于 03:55:41 记录账号 2221 的 `grok-4.7` 流式用量 |

最终 CLI 返回的结构化校验值为模型 `grok-4.7`、Router 端口 13083、Sub2 端口 13080、
端口校验和 26163、release token `09e1544d2732-13083`。
本次未创建遗留的测试分组或 API Key。

## 全量账号与出口

| 核验项 | 数量 |
| --- | ---: |
| 总账号 | 309 |
| enabled + active | 294 |
| 本次全量账单刷新通过 | 294 |
| 有效账号中已过期凭据 | 0 |
| 有效账号中永久刷新失败 | 0 |
| 有效账号中当前冷却 | 0 |
| 额度耗尽及模型封锁 | 0 |
| `invalid_grant / reauthRequired` | 15 |

294 个账单快照符合上游未返回套餐名的 Free 账户信号。其付费额度字段为零，
不能据此认定免费生成额度耗尽。全量刷新中 117、144、177 各有一次临时失败，
停批检查后分别单次复核成功，最终覆盖率为 294/294。

Resin API 先核对 `items == total`，再将全量 lease 身份集合与数据库推导的
`grok_build_<account_id>` 集合做双向差集：expected=309、matched=309、missing=0、extra=0。
309 个独立固定身份对应 256 个节点、207 个不同公网 IP，没有缺少节点或 IP 的 lease。
独立绑定允许共享物理出口，不提供每账号独占公网 IP 的保证。

Guard 已修复误处理非受管平台请求的问题，避免其他平台日志触发无关出口操作。
当前 guard 随运维职责迁移至 `/opt/resin-operations`；RELEASE.json 指向
`37335a48cde7343c738f0db7d9b01076505a4de7`，包含于 Resin 的已推送 `origin/mine`。
运行脚本与该提交对应文件的 SHA-256 完全相同。

## 15 个待重新授权账号

```text
13, 14, 16, 18, 19, 21, 31, 32, 49, 50, 107, 110, 119, 122, 161
```

这些账号仍为 `reauthRequired`，本次没有恢复成功的账号。
账号 50 的协议路径止于 `oauth_redirect_incomplete`；新的隔离浏览器路径完成 Resin HTTP CONNECT
预检后止于 `oauth_timeout`。没有生成可导入的授权，也没有向其他身份回退或批量重试。
不能据此推断密码错误或账号被封禁；确定的是旧 refresh token 已失效，自动登录尚未取得新授权。

已交付 [单账号重新授权工具及操作说明](grok2api-reauthorization.zh-CN.md)：
匹配原身份与原注册材料、核对一致备份、使用新的私有浏览器目录，并在导出前后校验身份。
浏览器模式已修正为无需无关的验证码服务密钥。
取得有效的新授权后，应按说明导入回原账号，完成模型核验，再恢复原先停用账号的调度。

## 清理与保留

- 旧 `grok-quality-retry-proxy.service`、18001 监听、Nginx map/转发及旧中间件已退役。
  当前 `/grok2api/v1/responses` 直接转发 18000。上游仍支持的可选 egress sidecar 保持禁用。
- 本次四个临时 Git 工作树及其已合并本地分支均通过 Git 正常移除；重新授权测试远端分支亦已删除。
  其他任务的分支、工作树和非任务改动不属于本次清理对象。
- 临时请求结果已提炼为本报告，由 storage-guard 按创建任务归属回收临时目录。
  数据库恢复点、旧服务的回滚资料和真实重新授权材料保留在受保护位置。
- 备份根目录：`/var/backups/grok-chain/20260922-01a0c4bc`。
  重新授权材料：`/root/grok-build-auth/private/runs/20260922-grok2api-reauth`。
  凭据、数据库、浏览器登录态与原始异常正文均不进入 Git。

账号接入桥服务已加载新提交，探针统一使用 Grok 4.7 / Build 1.0.40，
重启后 `/health` 为 HTTP 200、服务 active，未留下本次恢复浏览器进程。
