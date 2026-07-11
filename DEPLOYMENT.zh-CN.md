# 服务器部署与回滚

本项目是一次性 CLI，不开放公网端口，不安装反向代理或常驻服务。部署版本固定在
Git commit；真实 OAuth、账号创建、验证码求解和额度探测不属于安装验证。

## 安装

```bash
sudo useradd --system --create-home --home-dir /opt/grok-build-auth \
  --shell /usr/sbin/nologin grokbuild 2>/dev/null || true
sudo install -d -o grokbuild -g grokbuild -m 0700 \
  /opt/grok-build-auth/app /opt/grok-build-auth/venv \
  /var/lib/grok-build-auth /var/lib/grok-build-auth/cliproxyapi-auth
sudo install -o root -g grokbuild -m 0640 /dev/null /etc/grok-build-auth.env

sudo -u grokbuild git clone <PRIVATE_HARDENED_REPOSITORY_URL> /opt/grok-build-auth/app
sudo -u grokbuild git -C /opt/grok-build-auth/app checkout <AUDITED_HARDENED_COMMIT>
sudo -u grokbuild python3 -m venv /opt/grok-build-auth/venv
sudo -u grokbuild /opt/grok-build-auth/venv/bin/pip install \
  --require-hashes -r /opt/grok-build-auth/app/requirements-lock.txt
```

`/etc/grok-build-auth.env` 只允许放当前路径实际需要的秘密，不提交 Git，不在命令行
展开。运行前使用受控 shell 读取该文件并保持 `umask 077`。

## 无副作用验收

```bash
sudo -u grokbuild /opt/grok-build-auth/venv/bin/python -m compileall -q \
  /opt/grok-build-auth/app
sudo -u grokbuild /opt/grok-build-auth/venv/bin/python -m unittest discover \
  -s /opt/grok-build-auth/app/tests -v
sudo -u grokbuild /opt/grok-build-auth/venv/bin/python \
  /opt/grok-build-auth/app/run.py --help
sudo -u grokbuild /opt/grok-build-auth/venv/bin/pip check
```

## 升级

```bash
sudo -u grokbuild git -C /opt/grok-build-auth/app fetch origin <NEW_AUDITED_COMMIT>
sudo -u grokbuild git -C /opt/grok-build-auth/app checkout <NEW_AUDITED_COMMIT>
sudo -u grokbuild /opt/grok-build-auth/venv/bin/pip install \
  --require-hashes -r /opt/grok-build-auth/app/requirements-lock.txt
```

不要自动跟随 `main`，每次升级先审查上游差异并重新运行全部无副作用验收。

## 回滚

```bash
sudo -u grokbuild git -C /opt/grok-build-auth/app checkout <PREVIOUS_AUDITED_COMMIT>
sudo -u grokbuild /opt/grok-build-auth/venv/bin/pip install \
  --require-hashes -r /opt/grok-build-auth/app/requirements-lock.txt
```

回滚源码不会删除或还原 auth 文件。CLIProxyAPI 若会刷新 token，应由其独立备份和
恢复流程管理，禁止通过 Git 传输 auth JSON。

## 首次真实运行确认清单

- 账号属于用户本人，且用户已明确授权本次 OAuth。
- 仅处理一个账号、一个线程，不创建定时任务。
- `/var/lib/grok-build-auth/cliproxyapi-auth` 为 `0700`，auth 文件为 `0600`。
- callback 仅绑定 `127.0.0.1` 或 `::1`，没有公网防火墙入站规则。
- token 目标固定为 `https://cli-chat-proxy.grok.com/v1`。
- 已说明浏览器授权、账号风控、第三方费用和额度消耗风险。
- 未启用调试日志或 Playwright 隐式回退。
- 只有在用户单独明确授权后才运行 `xai_build_quota_probe.py`。
