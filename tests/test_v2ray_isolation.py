import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_v2ray_isolation_test", ROOT / "scripts" / "check_v2ray_isolation.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def pool_payload(docker_listen="172.18.0.1"):
    inbounds = []
    rules = []
    for number, port in enumerate(range(10900, 10908), 1):
        inbounds.extend([
            {"tag": f"in-proxy-{number:02d}", "listen": "127.0.0.1", "port": port, "protocol": "socks", "settings": {"auth": "noauth"}},
            {"tag": f"in-docker-proxy-{number:02d}", "listen": docker_listen, "port": port, "protocol": "socks", "settings": {"auth": "password", "accounts": [{"user": "u", "pass": "p"}]}},
        ])
        rules.append({"inboundTag": [f"in-docker-proxy-{number:02d}"], "outboundTag": f"proxy-{number:02d}"})
    return {"inbounds": inbounds, "routing": {"rules": rules}}


def test_pool_config_policy_rejects_public_or_unauthenticated_inbounds(tmp_path):
    path = tmp_path / "pool.json"
    payload = pool_payload()
    path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    assert all(MODULE.pool_config_policy(path, "172.18.0.1").values())

    payload["inbounds"][1]["settings"] = {"auth": "noauth"}
    payload["inbounds"].append({"listen": "0.0.0.0", "port": 10900})
    path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    result = MODULE.pool_config_policy(path, "172.18.0.1")
    assert result["docker_auth"] is False
    assert result["no_extra"] is False


def test_listener_policy_requires_pool_pid_and_rejects_wildcard():
    lines = []
    for address in ("127.0.0.1", "172.18.0.1"):
        for port in range(10900, 10908):
            lines.append(f'LISTEN 0 4096 {address}:{port} 0.0.0.0:* users:(("v2ray",pid=123,fd=1))')
    assert all(MODULE.listener_policy("\n".join(lines), "123", "172.18.0.1").values())
    lines.append('LISTEN 0 4096 0.0.0.0:10900 0.0.0.0:* users:(("v2ray",pid=123,fd=2))')
    assert MODULE.listener_policy("\n".join(lines), "123", "172.18.0.1")["no_extra"] is False
