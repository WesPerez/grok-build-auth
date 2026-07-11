# Grok 代理池运维与健康检查

日期：2026-07-12

## 结论

原始资料中确实包含一份 235 条 Clash 风格静态代理记录。帖子标题声称“分享 200 个节点，直连大概 100 个可用”，但正文没有逐节点健康结果、测试时间或稳定性证据。因此 235 是可核对的配置条目数，“约 100 个可用”只是来源作者当时的陈述。

Resin 不是这 235 个节点的供应商。它是一个订阅导入和调度层，通过 `Platform.Account` 形式的用户名生成粘性会话。外部项目只生成不同 Account，没有验证出口 IP，也没有保证 OAuth 和后续请求继续使用同一 Account。

## 原始资料盘点

来源：LINUX DO 主题 `2556512` 的已保存可见正文。原始文本因包含第三方节点凭据，未纳入本仓库。

- 静态代理记录：235 条。
- 不同 `server:port`：235 个。
- 不同 server：176 个。
- 协议：SS 86、Trojan 48、AnyTLS 41、HTTP 21、VMess 16、VLESS 13、Hysteria2 9、TUIC 1。
- 帖子标题的“约 100 个直连可用”未经本机复测。
- 正文含 password、UUID、auth、username、public-key 等敏感字段，不应复制到普通文档、manifest 或 Git。

这些记录来自论坛免费节点分享，没有证据证明其属于单一正式供应商，也没有证据证明当前仍受原分享者授权、在线或安全。当前服务器未安装 Mihomo、Clash、sing-box、Xray 客户端，不能直接按混合协议清单进行统一测试。

## Resin 对照

`grok_bytao` 的实现方式：

1. 把订阅导入 Resin。
2. 使用一个 HTTP 代理入口。
3. 每个账号把用户名改为 `Platform.Account`，Account 含 worker/序号和随机值。
4. 浏览器通过本地认证代理桥连接 Resin。

不能照搬的部分：

- 没有出口 IP 探针，无法证明一号一 IP。
- 代理失败后会回退直连。
- CPA/OIDC mint 没有继承注册阶段的动态 Account。
- 归档配置把 CPA 代理设置为 direct。

当前 GROKAUTH 的正确约束是：注册、SSO、OAuth、Build probe、Sub2API refresh 和业务请求全部绑定同一 `proxy_ref`；失败必须 fail closed。

## 2026-07-12 服务器审计样本

探测目标仅为 Cloudflare HTTPS trace，没有访问 x.ai。

| 入口 | 结果 | TLS | 地区 | 三次延迟 | 判断 |
|---|---|---|---|---|---|
| legacy authenticated SOCKS | 3/3 成功 | TLS 1.3 | FR | 608.3 / 615.1 / 638.6 ms | 健康 |
| browser local SOCKS | 3/3 成功 | TLS 1.3 | FR | 608.8 / 609.8 / 611.0 ms | 健康 |

合并检查判定两个入口出口重复，因此第二个 ref 被拒绝。完整 IP 和代理 URL未写入本报告。

该结果只是当时的部署快照，不是仓库对任何服务器的永久保证。该上游此前出现过 TLS EOF 和连接超时，所以单次健康不能代替持续监测。

## 已落地代码

新增：

- `xconsole_client/proxy_health.py`
- `scripts/check_proxy_pool.py`
- `tests/test_proxy_health.py`

批处理启动顺序：

```text
加载 0600 proxy pool
-> 校验 Sub2API ProxyID
-> 每节点 3 次 Cloudflare HTTPS trace
-> 校验证书、全局 IP、TLS、成功率和出口稳定性
-> 检查不同 ref 是否重复出口
-> 生成脱敏健康快照
-> 才允许创建邮箱和开始注册
```

失败策略：

- 少于 2/3 成功：拒绝整批。
- 同一节点多次出口漂移：拒绝整批。
- 两个 ref 实际同一出口：拒绝整批。
- TLS 未验证或返回私网 IP：拒绝整批。
- 不回退 legacy proxy 或宿主直连。
- manifest 不保存 URL、账号密码或完整 IP。

手动检查命令：

```bash
python3 scripts/check_proxy_pool.py --attempts 3 --timeout 10
```

只有配置 `GROK_PROXY_POOL_FILE` 后该命令才会运行节点池检查。示例结构见：

```text
private.example/proxies.example.json
```

## 浏览器后端停止条件

浏览器注册保留为 1 路 canary，并已实现：

- Xvfb 有头 Microsoft Edge。
- 独立 browser/context。
- 新版 `Sign up with email` 入口。
- OTP 页面推进等待。
- Cloudflare block 明确识别。
- 代理失败不回退直连。

遇到以下任一情况立即停止当前 attempt，不自动更换 IP 重试：

- Cloudflare `Sorry, you have been blocked`。
- `Blocked due to abusive traffic patterns`。
- 邮箱 OTP 在超时内未到达。
- 代理 TLS/连接失败。
- 出口与本批健康快照不一致。

## 启用多节点的输入要求

若要正式启用多节点，必须先提供以下任一种受控输入：

1. 用户自有且仍有效的 Mihomo/Clash 订阅，并明确授权做中立健康检查。
2. 用户自有 Resin 实例及订阅已导入的证明。
3. 至少两个独立 HTTP/SOCKS 代理 URL，以及它们在 Sub2API 中对应的 ProxyID。

每个节点的生产配置只写：

```json
{
  "ref": "node-01",
  "url_env": "GROK_PROXY_NODE_1",
  "enabled": true,
  "max_active_leases": 1,
  "sub2api_proxy_id": 12
}
```

实际 URL 只能放在权限为 `0600` 的 `private/runtime.env` 或环境变量中。

运维和自动化工具不应：

- 直接复制论坛节点凭据到仓库。
- 把不同入口误称为不同出口。
- 只随机 Resin Account 就声称一号一 IP。
- 在健康失败后切换直连。
- 在 x.ai 封禁或不发 OTP 时自动轮换节点继续注册。

## 研究覆盖限制

- 已完整读取当时保存的相关可见正文，原始含凭据文件已不作为仓库产物。
- Resin 主题只保存到可见楼层 7，页面明确还有“加载更多帖子”。
- 本地 reader 缓存是 404 外壳，未补齐全部论坛回复。
- 本轮没有使用浏览器标签页，没有下载新附件，也没有访问 x.ai。
- 235 条论坛节点只做结构统计，没有使用其凭据建立连接。
