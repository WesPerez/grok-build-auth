import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_v2ray_isolation_test", ROOT / "scripts" / "check_v2ray_isolation.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def pool_payload():
    inbounds = []
    outbounds = []
    rules = []
    for number, port in enumerate(range(10900, 10908), 1):
        inbounds.append({"tag": f"in-proxy-{number:02d}", "listen": "127.0.0.1", "port": port, "protocol": "socks", "settings": {"auth": "noauth"}})
        outbounds.append({"tag": f"proxy-{number:02d}", "protocol": "socks"})
        rules.append({"inboundTag": [f"in-proxy-{number:02d}"], "outboundTag": f"proxy-{number:02d}"})
    return {"inbounds": inbounds, "outbounds": outbounds, "routing": {"rules": rules}}


def test_pool_config_policy_rejects_public_inbounds(tmp_path):
    path = tmp_path / "pool.json"
    payload = pool_payload()
    path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    assert all(MODULE.pool_config_policy(path).values())

    payload["inbounds"].append({"listen": "0.0.0.0", "port": 10900})
    path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    result = MODULE.pool_config_policy(path)
    assert result["no_extra"] is False


def test_pool_config_policy_rejects_wrong_tag_and_dangling_route(tmp_path):
    path = tmp_path / "pool.json"
    payload = pool_payload()
    payload["inbounds"][0]["tag"] = "wrong-tag"
    path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    assert MODULE.pool_config_policy(path)["loopback"] is False

    payload = pool_payload()
    payload["outbounds"] = payload["outbounds"][1:]
    path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    assert MODULE.pool_config_policy(path)["routing"] is False


def test_pool_config_policy_rejects_duplicate_or_conflicting_route(tmp_path):
    path = tmp_path / "pool.json"
    payload = pool_payload()
    payload["routing"]["rules"].append({
        "inboundTag": ["in-proxy-01"],
        "outboundTag": "proxy-02",
    })
    path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    assert MODULE.pool_config_policy(path)["routing"] is False


def test_listener_policy_requires_pool_pid_and_rejects_wildcard():
    lines = []
    for port in range(10900, 10908):
        lines.append(f'LISTEN 0 4096 127.0.0.1:{port} 0.0.0.0:* users:(("v2ray",pid=123,fd=1))')
    assert all(MODULE.listener_policy("\n".join(lines), "123").values())
    lines.append('LISTEN 0 4096 0.0.0.0:10900 0.0.0.0:* users:(("v2ray",pid=123,fd=2))')
    assert MODULE.listener_policy("\n".join(lines), "123")["no_extra"] is False
