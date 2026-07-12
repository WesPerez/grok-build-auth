# Grok 代理池部署与使用手册

日期：2026-07-12
版本：1.0

---

## 一、架构

```
register_and_import.py (批处理编排器)
  │
  ├─ proxy_pool.acquire()  → 轮询选一个空闲节点，获取 lease
  ├─ lease.url 作为 GROK_ATTEMPT_PROXY_URL 传给 run.py 子进程
  ├─ run.py 全程使用同一出口：cookie → 邮箱 → Turnstile → 建号 → SSO → OAuth
  ├─ 完成后 proxy_pool.release(lease)，释放节点
  └─ 导入前探针读取同一 proxy_ref，保持出口一致
```

每个节点 `max_active_leases=1`，同一时间只服务一个账号。节点不可用时 fail-closed，不回退直连。

---

## 二、代理池节点

| 本地 SOCKS5 端口 | 协议 | 上游出口 IP | 国家 | 延迟 |
|---|---|---|---|---|
| 127.0.0.1:10900 | VMess | 168.138.43.75 | JP | ~450ms |
| 127.0.0.1:10901 | VMess | 165.154.195.38 | TW | ~500ms |
| 127.0.0.1:10902 | VMess | 152.67.8.205 | IN | ~1180ms |
| 127.0.0.1:10903 | Trojan | 149.104.104.58 | PK | ~1095ms |
| 127.0.0.1:10904 | Trojan | 23.251.51.63 | DE | ~628ms |
| 127.0.0.1:10905 | Trojan | 51.158.230.54 | NL | ~664ms |
| 127.0.0.1:10906 | Trojan | 151.115.33.179 | PL | ~738ms |
| 127.0.0.1:10907 | Trojan | 51.15.135.251 | FR | ~648ms |

初次部署时 8 个节点均为 TLS 1.3 和唯一出口 IP。2026-07-12 复测发现
`node-05`、`node-07` 间歇性不可用；当前生产 canary 只启用稳定的
`node-01`，其余节点必须重新通过 3 次健康检查后再逐个启用。

---

## 三、配置文件

### 3.1 V2Ray 代理池配置

**文件：** `/etc/v2ray/grok_pool.json`

8 个 SOCKS5 入站（10900-10907），每个通过独立出站路由到对应上游节点。完整配置见该文件。

### 3.2 systemd 服务

**文件：** `/etc/systemd/system/v2ray-grok-pool.service`

```ini
[Unit]
Description=V2Ray Grok Proxy Pool
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/v2ray/v2ray run -config /etc/v2ray/grok_pool.json
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**管理命令：**
```bash
systemctl start v2ray-grok-pool    # 启动
systemctl stop v2ray-grok-pool     # 停止
systemctl status v2ray-grok-pool   # 状态
systemctl enable v2ray-grok-pool   # 开机自启
```

### 3.3 GROKAUTH 代理池配置

**文件：** `/root/grok-build-auth-review/private/proxies.json`

```json
{
  "version": 1,
  "proxies": [
    {"ref": "node-01", "url_env": "GROK_PROXY_NODE_1", "enabled": true, "max_active_leases": 1},
    {"ref": "node-02", "url_env": "GROK_PROXY_NODE_2", "enabled": false, "max_active_leases": 1},
    {"ref": "node-03", "url_env": "GROK_PROXY_NODE_3", "enabled": false, "max_active_leases": 1},
    {"ref": "node-04", "url_env": "GROK_PROXY_NODE_4", "enabled": false, "max_active_leases": 1},
    {"ref": "node-05", "url_env": "GROK_PROXY_NODE_5", "enabled": false, "max_active_leases": 1},
    {"ref": "node-06", "url_env": "GROK_PROXY_NODE_6", "enabled": false, "max_active_leases": 1},
    {"ref": "node-07", "url_env": "GROK_PROXY_NODE_7", "enabled": false, "max_active_leases": 1},
    {"ref": "node-08", "url_env": "GROK_PROXY_NODE_8", "enabled": false, "max_active_leases": 1}
  ]
}
```

权限必须为 `0600`。

### 3.4 环境变量

**文件：** `/root/grok-build-auth-review/private/runtime.env`（追加内容）

```bash
GROK_PROXY_POOL_FILE=/root/grok-build-auth-review/private/proxies.json
GROK_ALLOW_MISSING_SUB2API_PROXY_IDS=true
GROK_PROXY_NODE_1=socks5://127.0.0.1:10900
GROK_PROXY_NODE_2=socks5://127.0.0.1:10901
GROK_PROXY_NODE_3=socks5://127.0.0.1:10902
GROK_PROXY_NODE_4=socks5://127.0.0.1:10903
GROK_PROXY_NODE_5=socks5://127.0.0.1:10904
GROK_PROXY_NODE_6=socks5://127.0.0.1:10905
GROK_PROXY_NODE_7=socks5://127.0.0.1:10906
GROK_PROXY_NODE_8=socks5://127.0.0.1:10907
GROK_BROWSER_PROXY_URL=socks5://127.0.0.1:10904
GROK_BROWSER_HEADED=true
```

`GROK_ALLOW_MISSING_SUB2API_PROXY_IDS=true` 表示注册和导入前探针保持同一
出口，但导入后的 Sub2API 请求不保证继续使用该出口。默认值为 `false`；只有
明确接受这个边界时才能开启，并会记录到批次 manifest 的
`proxy_pool.postimport_stickiness=false`。

---

## 四、验证命令

### 4.1 检查代理池是否运行
```bash
systemctl is-active v2ray-grok-pool
ss -ltnp | rg '1090'
```

### 4.2 测试单个节点
```bash
curl --socks5-hostname 127.0.0.1:10900 --max-time 10 -fsS https://api.ipify.org
# 应返回: 168.138.43.75
```

### 4.3 批量测试全部节点
```bash
for port in 10900 10901 10902 10903 10904 10905 10906 10907; do
    ip=$(curl --socks5-hostname 127.0.0.1:$port --max-time 8 -fsS https://api.ipify.org 2>/dev/null)
    echo "port $port: ${ip:-FAIL}"
done
```

### 4.4 运行代理池健康检查
```bash
cd /root/grok-build-auth-review
python3 scripts/check_proxy_pool.py
```

### 4.5 检查 x.ai 注册页是否可访问
```bash
cd /root/grok-build-auth-review
python3 -c "
import sys; sys.path.insert(0,'.')
from playwright.sync_api import sync_playwright
from xconsole_client.registration_backends import _edge_executable
with sync_playwright() as p:
    b = p.chromium.launch(headless=False, executable_path=_edge_executable(),
        proxy={'server': 'socks5://127.0.0.1:10900'})
    page = b.new_context(locale='en-US').new_page()
    page.goto('https://accounts.x.ai/sign-up', timeout=30000, wait_until='domcontentloaded')
    print('TITLE:', page.title())
    print('BODY:', page.locator('body').inner_text(timeout=5000)[:200])
    b.close()
"
```

---

## 五、使用方式

### 5.1 协议注册（YesCaptcha Turnstile）
```bash
cd /root/grok-build-auth-review
python3 scripts/register_and_import.py \
    --count 6 --workers 2 \
    --registration-backend protocol-yescaptcha \
    --import-partial \
    --cleanup-failed-mailboxes \
    --confirm-production-write
```

每个账号自动轮询领取不同节点，全程 sticky 同一出口。

### 5.2 浏览器注册（Playwright + Edge）
```bash
cd /root/grok-build-auth-review
python3 scripts/register_and_import.py \
    --count 1 --workers 1 \
    --registration-backend browser-playwright-edge \
    --import-partial \
    --cleanup-failed-mailboxes \
    --confirm-production-write
```

浏览器模式固定 1 路并发，使用 `GROK_BROWSER_PROXY_URL` 指定的节点。

### 5.3 Web 控制台
访问 `http://127.0.0.1:17860`，在页面上选择注册方式和数量，点击开始。

---

## 六、添加新节点

1. 在 `/etc/v2ray/grok_pool.json` 中添加新的 inbound + outbound
2. 在 `private/proxies.json` 中添加新的 `{"ref": "node-NN", "url_env": "GROK_PROXY_NODE_N", ...}`
3. 在 `private/runtime.env` 中添加 `GROK_PROXY_NODE_N=socks5://127.0.0.1:新端口`
4. 重启 V2Ray：`systemctl restart v2ray-grok-pool`
5. 运行健康检查：`python3 scripts/check_proxy_pool.py`

---

## 七、与主 V2Ray 的关系

代理池 V2Ray（10900-10907）和主 V2Ray（10808/10810）是两个独立进程，互不干扰：

- 主 V2Ray：10808（Docker 网桥）+ 10810（本地），用于 Sub2API 和日常代理
- 代理池 V2Ray：10900-10907，仅用于 Grok 注册

---

## 八、测试结果

- 源文件：235 条 Clash 节点
- 已测试：184 条（跳过 anytls/hysteria2/tuic 共 51 条）
- 可用：62 条（Trojan 46 + VMess 16）
- 唯一出口 IP：24 个
- 初次代理池：8 个不同国家和出口
- 当前 canary：仅 `node-01` 启用，3/3 健康检查通过，TLS 1.3，出口稳定
- 协议注册 canary：德国节点 25 秒完成建号 + SSO + OAuth
- 浏览器注册 canary：日本/台湾节点可打开注册页，当前 x.ai 抑制验证码发送
