# Grok Build Auth 服务器批量运行手册

> 本文只包含可公开的部署方法。真实域名、地址、密码、API Key、代理 URL、token 和生产路径必须放在本机 `private/` 中，不得提交。

## 1. 适用范围

本机扩展流程由三部分组成：

1. 通过 Mailu 官方 CLI 创建独立 IMAP 邮箱。
2. 逐个运行 `run.py -e imap`，完成注册、OAuth 和 auth JSON 导出。
3. 将成功 auth 聚合为一个 Sub2API bundle，整批只调用一次导入工具。

最后一步会写入生产 Sub2API。脚本要求显式传入
`--confirm-production-write`，并由导入工具在写入前创建一个 PostgreSQL
备份。无论批次指定 1 次还是 100 次，每批只产生一个备份。

## 2. 文件布局

```text
grok-build-auth/
├── register_and_import.sh
├── scripts/register_and_import.py
├── private.example/              # 可提交的空模板
└── private/                      # 0700，整目录 gitignored
    ├── runtime.env               # 0600，脚本唯一秘密配置来源
    ├── SECRETS.md                # 0600，本机凭据台账
    ├── artifacts/
    │   ├── auth/
    │   ├── sso/
    │   ├── bundles/
    │   └── backups/sub2api/
    └── runs/<batch-id>/
        ├── auth/<mailbox>/
        ├── results/
        ├── logs/
        ├── bundle/sub2api-bundle.json
        ├── import/result.json
        ├── backup/
        └── manifest.json
```

`results/*.json` 会保存生成账号的密码，`auth/` 和 `bundle/` 包含 OAuth
token，所有这些文件都必须保持 `0600`。

## 3. 依赖与版本

- Python 3.12
- Docker CLI
- Mailu，且 `mailu-admin` 容器提供 `flask mailu user` 和
  `flask mailu user-delete`
- PostgreSQL/Sub2API Docker 部署
- `sub2api_live_tool.py`
- v2ray 或其他可用的 HTTP/SOCKS 代理
- Python 依赖优先使用 `requirements-lock.txt`

安装：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-lock.txt
```

生产批次的 `manifest.json` 会记录批次号、尝试数、成功/失败数、bundle
hash、导入 ID 和备份信息。镜像版本与部署版本应由运维系统另行记录。

## 4. 私有配置

初始化：

```bash
mkdir -p private
chmod 700 private
cp private.example/runtime.env.example private/runtime.env
cp private.example/SECRETS.example.md private/SECRETS.md
chmod 600 private/runtime.env private/SECRETS.md
```

`private/runtime.env` 必须配置：

```bash
YESCAPTCHA_API_KEY=<secret>
IMAP_SERVER=<imap-host>
IMAP_PORT=143
IMAP_SSL=false
IMAP_PASSWORD=<secret>
HTTPS_PROXY=<authenticated-proxy-url>
HTTP_PROXY=<authenticated-proxy-url>

MAILU_DOMAIN=<mail-domain>
MAILU_DB=<mailu-sqlite-path>
MAILU_ADMIN_CONTAINER=<mailu-admin-container>
MAILU_FLASK_BIN=/app/venv/bin/flask
MAILU_IMAP_CONTAINER=<mailu-imap-container>
MAILU_MAIL_ROOT=/mail

SUB2API_ENV=<sub2api-deployment-env>
SUB2API_URL=<local-admin-api-url>
SUB2API_GROUP=openai
SUB2API_POSTGRES_CONTAINER=<postgres-container>
SUB2API_PG_USER=<postgres-user>
SUB2API_PG_DB=<postgres-database>
SUB2API_IMPORT_TOOL=<absolute-path-to-sub2api_live_tool.py>
```

不要在命令行参数中传密码，以免进入 shell history。脚本只从
`private/runtime.env` 读取秘密，并以进程环境变量向 `run.py` 传递本轮
邮箱配置，不再修改公共 `.env`。

## 5. 单次注册结果

`run.py` 支持机器结果文件：

```bash
python3 run.py -e imap \
  --cliproxyapi-auth-dir /secure/auth-dir \
  --result-json /secure/result.json
```

行为保证：

- 结果使用临时文件加 `os.replace` 原子写入。
- 文件权限为 `0600`。
- 失败时进程退出码为 `1`。
- OAuth 开启时，只有 auth 文件实际存在才算成功。
- 结果包含本次精确 auth 路径，不按“最新 JSON”猜测。

## 6. 批量运行

### 推荐：网页控制台

日常运行不需要记命令参数。启动：

```bash
bash start_web_console.sh
```

在服务器桌面浏览器访问 `http://127.0.0.1:17860`。控制台只监听本机回环地址，
提供以下功能：

- 启动前自动检查私有配置、权限、Mailu、PostgreSQL、导入工具和 Sub2API 地址。
- 只填写注册数量即可开始，生产写入前显示明确确认。
- 显示每个账号当前处于邮箱、验证、Turnstile、账号创建、SSO、OAuth 或导入阶段。
- 实时显示成功、失败、剩余、已导入数量及后台日志。
- 保存并展示历史批次、失败步骤、异常摘要和备份状态。
- 对 `import-failed` 批次提供“继续导入”，复用已有 auth 和 bundle，不重新注册。

可选 systemd 服务模板位于 `deploy/grok-batch-console.service`。

### 命令行备用入口

生产导入一个账号：

```bash
bash register_and_import.sh --count 1 --confirm-production-write
```

指定尝试次数：

```bash
bash register_and_import.sh --count 100 --confirm-production-write
```

`--count` 表示尝试次数，不保证全部成功。默认行为是继续尝试，但连续 3
次失败后停止；只要出现失败，就不导入成功子集。常用参数：

```text
--failure-policy abort|continue
--max-consecutive-failures N
--import-partial
--cleanup-failed-mailboxes
--no-import
--confirm-production-write
```

- `--import-partial`：明确允许失败批次中的成功账号进入生产。
- `--cleanup-failed-mailboxes`：仅在上游账号尚未创建时，通过 Mailu 官方
  CLI 精确删除本批次邮箱，再删除该邮箱的唯一 Maildir。已经创建上游
  账号的邮箱始终保留。
- `--no-import`：只注册并生成 bundle，不写 Sub2API，也不创建数据库备份。

## 7. 批次执行顺序

1. 校验 `private/runtime.env` 存在、权限为 `0600`、必需字段非空。
2. 创建唯一批次目录和 `manifest.json`。
3. 为每次尝试生成 `xai<随机值>@<MAILU_DOMAIN>`。
4. 用 `flask mailu user` 创建邮箱，并通过只读 SQLite 查询确认唯一记录。
5. 以独立 auth 目录和结果文件运行一次 `run.py`。
6. 校验退出码、结果邮箱、auth 路径边界、auth 内邮箱及 access token。
7. 将成功 auth 聚合成一个 bundle，`exported_at` 使用当前 UTC 时间。
8. 根据失败策略决定是否导入。
9. 只调用一次 `sub2api_live_tool.py import`。
10. 导入工具创建一次数据库备份、调用管理 API、绑定目标组并验证。
11. 按精确导入 ID 验证 `platform=grok`、`type=oauth`、`status=active`、
    `schedulable=true` 且已绑定目标组，全部匹配时才打印 `DONE`。

该验证证明数据库行和分组状态正常，不等于上游账号可用性验证。脚本不会
自动调用额度、配额或模型探测接口，manifest 会明确记录 `not-run`。

## 8. 失败与恢复

| 故障 | 默认结果 | 可重试点 |
|---|---|---|
| Mailu 创建失败 | 当前尝试失败，不启动注册 | 修复 Mailu 后重新开批次 |
| 验证码/Turnstile 失败 | 保留日志和结果，批次计失败 | 新批次重试 |
| 上游账号已创建但 OAuth 失败 | 保留邮箱，不自动删除 | 使用现有账号单独补 OAuth |
| auth 缺失或邮箱不匹配 | 拒绝生成 bundle | 检查本轮日志 |
| 部分注册失败 | 默认跳过生产导入 | 检查后重新运行，或显式 `--import-partial` |
| 备份失败 | 导入工具在写入前退出 | 修复磁盘/数据库后重试 |
| 导入返回部分失败 | 不打印 `DONE`，保留整批前备份 | 按 manifest 和 token hash 精确核对 |

Sub2API API 导入不是跨服务事务。发生部分写入时，脚本不会自动恢复生产
数据库。恢复必须由用户明确指定备份、目标容器和允许的数据丢失窗口后，
再按照受控发布流程执行。

## 9. 幂等与重复项

- 每个批次和邮箱均使用随机 ID。
- 每次注册有独立的空 auth 目录。
- bundle 使用 access token SHA-256 作为强重复标识。
- 导入工具在备份前扫描 active 和 deleted 行中的 token hash。
- 不允许用文件时间或 wildcard 选择 auth。
- 重跑前查看上一批 `manifest.json`，不要重复提交已完成 bundle。

## 10. 产物生命周期

长期保留：

- `manifest.json`
- 原始 auth JSON
- 导入结果
- 每批唯一数据库备份，至少保留到导入验收完成

可在确认不再需要后归档：

- bundle，因为它可以从 auth 重新生成
- SSO 调试文件
- 已完成批次的详细日志

不得按扩展名或邮箱前缀批量清理。只能处理 manifest 明确记录、且能证明
属于具体批次的目标。

## 11. 凭据轮换与 Git 历史

一旦秘密进入公开提交，必须：

1. 撤销或轮换对应 API Key、邮箱密码和代理凭据。
2. 将公开文档改为占位符。
3. 重写包含秘密的提交并使用 `git push --force-with-lease` 更新分支。
4. 通知已有 clone 丢弃旧分支历史后重新获取。
5. 使用 Git 扫描确认历史中不再出现非空秘密。

只追加“删除秘密”的新提交不能消除历史泄露。

## 12. 提交前检查

```bash
git status --short
git ls-files | grep -E '(^|/)(private|\.env)(/|$)' && exit 1 || true
git grep -n -E 'YESCAPTCHA_API_KEY=.+|IMAP_PASSWORD=.+|socks5h?://[^ ]+@'
python3 -m pytest -q
```

检查结果中只能出现空模板或 `<secret>` 占位符。
