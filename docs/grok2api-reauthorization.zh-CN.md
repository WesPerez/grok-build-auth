# Grok2API 单账号重新授权

`scripts/reauthorize_grok2api_account.py` 为已存在且明确返回 `invalid_grant` 的
Grok Build 账号生成新授权。默认只读；不会注册新账号、刷新历史 auth、改写原批次、
导入或删除账号。现有批次恢复脚本只补缺失 auth，不适合重授权已有失效导出。

先查看计划：

```bash
python3 scripts/reauthorize_grok2api_account.py --account-id <ID>
```

计划要求数据库中的 email、user ID 和 team ID 完整且身份 hash 自洽；在历史 manifest
中按完整 email 唯一匹配，在对应 results 中再次按完整 email 匹配，不能默认取第一行。
密码只存在进程内，原注册 Resin v2 代理必须仍可解析。计划不会发送网络请求。

确认计划和一致的 SQLite 恢复点后执行一次：

```bash
python3 scripts/reauthorize_grok2api_account.py --account-id <ID> \
  --expected-identity <计划中的 identity_sha256> \
  --backup <已核验的一致 SQLite 备份> \
  --output-dir /root/grok-build-auth/private/runs/<恢复批次>/<ID> \
  --execute
```

输出目录必须是尚不存在的 `private/runs` 子目录。原生 token 与兼容 auth 都存入该目录，
目录权限 0700、文件 0600。协议失败不重试、不切浏览器；超时或中断后先检查这个目录，
不可把不确定结果当成零副作用后自动重放。进程输出只包含账号 ID、状态、固定分类及 hash。

生成后按 Grok2API 导入规则校验 `sub` 与 team：team 优先取显式字段，否则从 ID token
（缺省时 access token）的 `team_id` 读取。身份不符时保留私有材料并停止，不能放宽匹配。
`exported_identity_verified` 只代表已生成且身份匹配，不代表已导入或账号可用。

后续操作须通过 Grok2API 管理 API：单账号导入，读回确认仍为原 ID、账号总数不增，
刷新/额度和模型检查成功后再启用原先停用的账号。保留原名称、出口绑定和冷却状态；
HTTP 200 中的错误事件不算成功。最终通过 Sub2API 与 Router 核对实际 Grok 模型及账号命中。
不要对 402/429 或网络错误账号使用这个重新授权入口。

验证使用合成数据：

```bash
python3 -m pytest -q tests/test_grok2api_reauthorization.py
```

GitHub Actions 执行相同检查。服务器若触发构建容量门禁，应使用 CI，不能绕过门禁本机重跑。
