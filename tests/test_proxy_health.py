from types import SimpleNamespace

import pytest

from xconsole_client.proxy_health import _parse_trace, check_proxy_pool_health
from xconsole_client.proxy_pool import ProxyPool, ProxyPoolError, ProxySpec


def pool(*refs):
    return ProxyPool([
        ProxySpec(ref=ref, url=f"http://{ref}.example:8080", sub2api_proxy_id=index + 1)
        for index, ref in enumerate(refs)
    ], configured=True)


def test_parse_trace_requires_global_ip_and_tls():
    assert _parse_trace("ip=8.8.8.8\nloc=US\ntls=TLSv1.3\n")["country"] == "US"
    with pytest.raises(ValueError):
        _parse_trace("ip=127.0.0.1\nloc=US\ntls=TLSv1.3\n")
    with pytest.raises(ValueError):
        _parse_trace("ip=8.8.8.8\nloc=US\n")


def test_health_snapshot_is_sanitized_and_sticky():
    snapshot = check_proxy_pool_health(
        pool("one"),
        probe_once=lambda spec, timeout: ("8.8.8.8", "US", "TLSv1.3", 12.5),
    )
    result = snapshot.results[0]
    assert result["healthy"] is True
    assert result["exit_hash"]
    assert "8.8.8.8" not in repr(snapshot)
    assert "one.example" not in repr(snapshot)


def test_health_rejects_exit_drift():
    values = iter([
        ("8.8.8.8", "US", "TLSv1.3", 10.0),
        ("1.1.1.1", "US", "TLSv1.3", 11.0),
        ("8.8.8.8", "US", "TLSv1.3", 12.0),
    ])
    with pytest.raises(ProxyPoolError, match="unhealthy"):
        check_proxy_pool_health(pool("one"), probe_once=lambda spec, timeout: next(values))


def test_health_rejects_duplicate_node_exits():
    with pytest.raises(ProxyPoolError, match="duplicate exits"):
        check_proxy_pool_health(
            pool("one", "two"),
            probe_once=lambda spec, timeout: ("8.8.8.8", "US", "TLSv1.3", 10.0),
        )


def test_health_rejects_too_many_failures():
    calls = 0
    def failing(spec, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ("8.8.8.8", "US", "TLSv1.3", 10.0)
        raise TimeoutError("timeout")
    with pytest.raises(ProxyPoolError, match="unhealthy"):
        check_proxy_pool_health(pool("one"), probe_once=failing)


def test_health_can_filter_unhealthy_nodes_for_registration():
    def probe(spec, timeout):
        if spec.ref == "bad":
            raise TimeoutError("down")
        return ("8.8.8.8", "US", "TLSv1.3", 10.0)

    snapshot = check_proxy_pool_health(
        pool("good", "bad"), probe_once=probe, require_all=False,
    )
    assert snapshot.healthy_refs == ("good",)
    assert snapshot.results[1]["reason"] == "insufficient-successes"
    assert pool("good", "bad").only_refs(set(snapshot.healthy_refs)).specs[0].ref == "good"


def test_health_records_duplicate_exit_without_exposing_ip():
    snapshot = check_proxy_pool_health(
        pool("one", "two"),
        probe_once=lambda spec, timeout: ("8.8.8.8", "US", "TLSv1.3", 10.0),
        require_all=False,
    )
    assert snapshot.healthy_refs == ("one",)
    assert snapshot.results[1]["reason"] == "duplicate-exit"
    assert snapshot.results[1]["duplicate_of"] == "one"
    assert "8.8.8.8" not in repr(snapshot)
