# Windows 客户端 Preprobe 与批量注册经验（2026-07-13）

## 背景

本会话目标：Windows 客户端注册 Grok 账号 → 铸造 CPA auth → 推送 hardened bridge / Sub2API。
服务器 agent 指出：真正的可用性门禁是 **Grok CLI `/v1/responses`**，不是 `/v1/models`；
客户端应把第一层 probe 前移，避免把大量不可用账号打到 bridge。

## 关键结论（证据驱动）

### 1. `/models` 不是可用性证明

- 本批 600 注册后，本地 `/models` verify 通过约 **434/493**，但 bridge 入库约 **70**。
- A/B（25 个本批新号）：`/models` 全是 ok，`/responses` 几乎全是 `permission-denied`。
- A/B（12 个已入库号）：`/responses` 可 pass/rate（额度），说明入库号确实能 chat。
- **结论**：`/models` HTTP 200 只证明 token 结构/网关可达，不证明 chat 资格。

### 2. 完整 CLI 请求头必须带齐

生产/配额探针（`xai_build_quota_probe.py`）使用：

```text
User-Agent: grok-cli/0.2.93
X-XAI-Token-Auth: xai-grok-cli
x-grok-client-version: 0.2.93
x-grok-client-identifier: grok-shell
```

A/B 在“已入库号”上 full vs min 头差异不大；但在未就绪号上两者都是 perm。
客户端 preprobe **固定使用完整头**，并以：

```text
HTTP 200 + status=completed + 响应 JSON 含标记文本
```

为 pass 条件。**禁止**“任意 2xx 即成功”。

### 3. TOS / tos-gate 是注册成功硬门槛

经验链路：

1. 仅 API `set_tos_accepted` 不够。
2. 必须浏览器 `browser_activate_chat_permission`：写 SSO cookie → 点同意 → **URL 离开 tos-gate**。
3. 点击评分必须避开 “Acceptable Use Policy” 链接误点。
4. 注册成功判定：`export_cpa` 成功；若开启 push，还要求 bridge `probe=passed`。

即使浏览器已离 gate，chat 资格仍可能延迟；因此 preprobe 的 `PERMISSION_DENIED` 应进 **pending** 复测，不能当废号立刻删除。

### 4. 推送与 finish 脚本踩坑

| 坑 | 现象 | 正确做法 |
|---|---|---|
| 日志编码 | 注册 CLI 日志常为 GBK | 解析时 gbk/cp936 优先 |
| HTTPError 读 body 超时 | finish 在 422 上 `e.read()` 卡住崩溃 | `e.read(limit)` + 捕获 TimeoutError |
| 进度卡死 | watcher/waiter 空转 | 任务 PID 归属清晰，只杀本任务进程 |
| `/models` 批量 verify | 假阳性高 | 用 `/responses` preprobe |
| 422 读作废号 | 误删可恢复账号 | PERMISSION_DENIED→pending；429→cooldown；revoked 才删 |
| 管理口代理 | 走 mint 代理导致怪错 | push 默认直连（`cpa_push_proxy=""`） |
| 并发 Chrome | 15 路约 150 Chrome | 批后清理本任务自动化浏览器；不杀用户浏览器 |
| 配置误删 | 清历史时差点删 config | **config.json 必须保留**；只清本任务产物 |

### 5. 决策矩阵（客户端 preprobe）

| decision | code 例 | 写哪里 | push? | 后续 |
|---|---|---|---|---|
| pass | PROBE_PASSED | `cpa_auths/` | 是 | bridge 仍做最终校验 |
| reject | MALFORMED_AUTH | 不写 auth | 否 | 记 `cpa_auth_failed.txt` |
| retry | PERMISSION_DENIED / NETWORK / 5xx | `cpa_pending/` | 否 | 批后 15 分钟内复测 |
| cooldown | RATE_LIMITED (402/429) | `cpa_cooldown/` | 否 | 等额度窗口，非废号 |
| refresh | TOKEN_INVALID | 先 refresh 一次；仍失败→pending | 否 | 明确 revoked 才删 |

**原则**：

- 只有 pass 才能进正式 `cpa_auths` 和 push。
- SSL / 代理抖动 / 超时 / 普通 403 / 5xx：**绝不删除**。
- bridge 仍是信任边界；客户端 preprobe 是减负与提纯，不是替代服务端。

## 代码落点

- `clients/windows/cpa/preprobe.py` — `probe_auth` / `try_refresh_access_token`
- `clients/windows/cpa_export.py` — mint 后、`write_cpa_xai_auth` 前接入
- `clients/windows/config.example.json` — `cpa_preprobe_*` 默认开启
- 注册门禁：`grok_register_ttk.py` 的 `set_tos_accepted` + `browser_activate_chat_permission`

## 批量结果备忘（600 / 并发 15）

- 注册成功 493 / 失败 107（约 82%）
- 本地 `/models` 434 通过（误导性高）
- bridge 入库约 70（422 PERMISSION_DENIED 为主）
- finish 曾在 push 中途因 422 body 读超时崩溃，后续 resume 修完

## 安全

- 失败日志只记 `code/status`，禁止 token / 响应全文。
- 不提交 `config.json`、`cpa_auths/`、`cpa_pending/`、`cpa_cooldown/`、`run_logs/`、账号明文。
- 清理只允许本任务有归属证据的自动化 Chrome / 本任务 PID。

## 建议操作顺序

1. 配置：`mint_required=true`，`cpa_preprobe_enabled=true`，按需 `cpa_push_enabled/required` + `cpa_require_probe_passed`。
2. 小批（1→3→10）验证 preprobe pass 率与 bridge 一致。
3. 大批注册；结束后对 `cpa_pending` 做有限窗口复测。
4. 用分组 Key 做最终 `/v1/responses` 验收。

## 10 账号充分测试（preprobe 上线后，2026-07-13 17:02）

配置：`register_count=10`，并发 3，`cpa_preprobe_enabled=true`，`cpa_push_required=true`。

| 指标 | 结果 |
|---|---|
| 注册槽位结束 | 成功 0 / 失败 10 |
| 正式 `cpa_auths` 新增 | **0**（正确：未 pass 不写正式目录） |
| `cpa_pending` | **10** |
| `cpa_cooldown` | 0 |
| preprobe 码 | PERMISSION_DENIED×6，PROBE_NETWORK_ERROR×4 |
| 远端 push | **0**（正确：未 pass 不 push） |
| 浏览器离 tos-gate | 多数账号已通过 |

解读：

1. **客户端 preprobe 门禁生效**：无假阳性入库、无 bridge 噪声请求。
2. 本窗口 chat 资格传播慢 + 代理/TLS 抖动，导致 pass=0；这与 600 批“本地 /models 高、bridge 422 高”一致，进一步证明不能用 `/models` 当成功。
3. pending 文件保留 token，可在 15 分钟窗口复测，**禁止当废号删除**。
4. 代码已加 `cpa_preprobe_attempts`（默认 3）对网络/权限类 soft-fail 短重试。

单元测试：`tests/test_windows_client_tools.py` 8 passed（含 preprobe 决策与 pending 不 push）。
