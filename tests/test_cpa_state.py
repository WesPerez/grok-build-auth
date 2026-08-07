import json
import os
import sys
import importlib.util
from argparse import Namespace
from pathlib import Path

import pytest

CLIENT = Path(__file__).resolve().parents[1] / "clients" / "windows"


def test_state_transition_is_exclusive_and_fingerprint_protected(tmp_path):
    sys.path.insert(0, str(CLIENT))
    try:
        from cpa.state import delete_if_fingerprint, fingerprint, push_fingerprint, transition
        first = {"email": "a@example.com", "sub": "one", "refresh_token": "old", "access_token": "a"}
        pending = transition(tmp_path, first, "pending")
        assert pending.exists()
        old_fp = fingerprint(first)
        second = dict(first, refresh_token="new", access_token="b")
        verified = transition(tmp_path, second, "verified")
        assert verified.exists() and not pending.exists()
        assert push_fingerprint(first) != push_fingerprint(second)
        assert delete_if_fingerprint(verified, old_fp, tmp_path) is False
        assert verified.exists()
        assert delete_if_fingerprint(verified, fingerprint(second), tmp_path) is True
    finally:
        sys.path.remove(str(CLIENT))


def test_refresh_rebuilds_metadata(monkeypatch):
    sys.path.insert(0, str(CLIENT))
    try:
        from cpa import preprobe
        rebuilt = preprobe._rebuild_refreshed_auth(
            {"email": "a@example.com", "refresh_token": "old", "base_url": "https://cli-chat-proxy.grok.com/v1",
             "expired": "stale", "last_refresh": "stale", "sub": "stale"},
            {"access_token": "not-jwt", "refresh_token": "new", "expires_in": 123},
        )
        assert rebuilt["refresh_token"] == "new"
        assert rebuilt["expires_in"] == 123
        assert rebuilt["expired"] != "stale"
        assert rebuilt["last_refresh"] != "stale"
        assert rebuilt["sub"] != "stale"
    finally:
        sys.path.remove(str(CLIENT))


def test_reprobe_promotes_and_pushes(tmp_path, monkeypatch):
    sys.path.insert(0, str(CLIENT))
    try:
        import cpa
        from cpa.state import transition
        payload = {"email": "pass@example.com", "sub": "one", "refresh_token": "refresh", "access_token": "access"}
        transition(tmp_path, payload, "pending")
        spec = importlib.util.spec_from_file_location("cpa_reprobe_test", CLIENT / "cpa_reprobe.py")
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        monkeypatch.setattr(cpa, "probe_auth", lambda *args, **kwargs: {
            "decision": "pass", "code": "PROBE_PASSED", "status": 200
        })
        monkeypatch.setattr(cpa, "push_auth_file", lambda **kwargs: (True, 200, "{}"))
        stats = module.run(Namespace(
            root=str(tmp_path), checkpoint="", proxy="", timeout=1,
            remote_base="https://bridge.example", remote_secret="secret",
            push_proxy="", insecure=False, push_timeout=1,
            workers=4, include_verified=False,
        ))
        assert stats["passed"] == 1 and stats["pushed"] == 1
        assert list((tmp_path / "cpa_auths").glob("xai-*.json"))
        assert not list((tmp_path / "cpa_pending").glob("xai-*.json"))
        assert (tmp_path / "cpa_reprobe_checkpoint.json").exists()
    finally:
        sys.path.remove(str(CLIENT))


def test_reprobe_concurrent_unique_and_verified_push_retry(tmp_path, monkeypatch):
    sys.path.insert(0, str(CLIENT))
    try:
        import cpa
        from cpa.state import transition
        for index in range(12):
            transition(tmp_path, {
                "email": f"u{index}@example.com", "sub": str(index),
                "refresh_token": f"r{index}", "access_token": f"a{index}",
            }, "pending")
        spec = importlib.util.spec_from_file_location("cpa_reprobe_concurrent_test", CLIENT / "cpa_reprobe.py")
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        seen = []
        monkeypatch.setattr(cpa, "probe_auth", lambda auth, **kwargs: (
            seen.append(auth["email"]) or {"decision": "pass", "code": "PROBE_PASSED", "status": 200}
        ))
        push_ok = {"value": False, "calls": 0}

        def push(**kwargs):
            push_ok["calls"] += 1
            return push_ok["value"], 200 if push_ok["value"] else 503, "{}"

        monkeypatch.setattr(cpa, "push_auth_file", push)
        common = dict(root=str(tmp_path), checkpoint="", proxy="", timeout=1,
                      remote_base="https://bridge.example", remote_secret="secret",
                      push_proxy="", insecure=False, push_timeout=1, workers=15)
        first = module.run(Namespace(**common, include_verified=False))
        assert first["scanned"] == 12
        assert first["passed"] == 12 and first["push_failed"] == 12
        assert len(seen) == len(set(seen)) == 12
        assert first["scanned"] == sum(first[k] for k in (
            "passed", "cooldown", "pending", "revoked_deleted", "revoked_preserved",
            "verified_skipped", "terminal_skipped", "errors",
        ))

        push_ok["value"] = True
        second = module.run(Namespace(**common, include_verified=True))
        assert second["scanned"] == 12 and second["pushed"] == 12
        calls_after_success = push_ok["calls"]
        third = module.run(Namespace(**common, include_verified=True))
        assert third["scanned"] == 12 and third["push_skipped"] == 12
        assert push_ok["calls"] == calls_after_success
        assert third["scanned"] == sum(third[k] for k in (
            "passed", "cooldown", "pending", "revoked_deleted", "revoked_preserved",
            "verified_skipped", "terminal_skipped", "errors",
        ))
    finally:
        sys.path.remove(str(CLIENT))


def test_verified_without_exact_failed_checkpoint_is_not_reprobed_or_pushed(tmp_path, monkeypatch):
    sys.path.insert(0, str(CLIENT))
    try:
        import cpa
        from cpa.state import transition
        payload = {"email": "old@example.com", "sub": "one", "refresh_token": "refresh", "access_token": "access"}
        transition(tmp_path, payload, "verified")
        spec = importlib.util.spec_from_file_location("cpa_reprobe_skip_test", CLIENT / "cpa_reprobe.py")
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        calls = {"probe": 0, "push": 0}
        monkeypatch.setattr(cpa, "probe_auth", lambda *args, **kwargs: calls.__setitem__("probe", calls["probe"] + 1))
        monkeypatch.setattr(cpa, "push_auth_file", lambda **kwargs: calls.__setitem__("push", calls["push"] + 1))
        stats = module.run(Namespace(
            root=str(tmp_path), checkpoint="", proxy="", timeout=1,
            remote_base="https://bridge.example", remote_secret="secret",
            push_proxy="", insecure=False, push_timeout=1,
            workers=1, include_verified=True,
        ))
        assert stats["verified_skipped"] == 1 and stats["push_skipped"] == 1
        assert calls == {"probe": 0, "push": 0}
    finally:
        sys.path.remove(str(CLIENT))


def test_verified_refresh_is_persisted_before_push(tmp_path, monkeypatch):
    sys.path.insert(0, str(CLIENT))
    try:
        import cpa
        from cpa.state import push_fingerprint, transition
        payload = {"email": "refresh@example.com", "sub": "one", "refresh_token": "old-refresh", "access_token": "old-access"}
        path = transition(tmp_path, payload, "verified")
        checkpoint = tmp_path / "checkpoint.json"
        checkpoint.write_text(json.dumps({str(path.resolve()): {
            "fingerprint": cpa.fingerprint(payload),
            "push_fingerprint": push_fingerprint(payload),
            "pushed": False,
        }}), encoding="utf-8")
        spec = importlib.util.spec_from_file_location("cpa_reprobe_refresh_test", CLIENT / "cpa_reprobe.py")
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        probes = iter((
            {"decision": "refresh", "code": "ACCESS_EXPIRED", "status": 401},
            {"decision": "pass", "code": "PROBE_PASSED", "status": 200},
        ))
        refreshed = dict(payload, access_token="new-access", refresh_token="new-refresh")
        monkeypatch.setattr(cpa, "probe_auth", lambda *args, **kwargs: next(probes))
        monkeypatch.setattr(cpa, "try_refresh_access_token", lambda *args, **kwargs: {"ok": True, "auth": refreshed})
        monkeypatch.setattr(cpa, "push_auth_file", lambda **kwargs: (True, 200, "{}"))
        stats = module.run(Namespace(
            root=str(tmp_path), checkpoint=str(checkpoint), proxy="", timeout=1,
            remote_base="https://bridge.example", remote_secret="secret",
            push_proxy="", insecure=False, push_timeout=1,
            workers=1, include_verified=True,
        ))
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert stats["pushed"] == 1
        assert saved["refresh_token"] == "new-refresh"
    finally:
        sys.path.remove(str(CLIENT))


def test_revoked_verified_auth_is_preserved(tmp_path, monkeypatch):
    sys.path.insert(0, str(CLIENT))
    try:
        import cpa
        from cpa.state import push_fingerprint, transition
        payload = {"email": "revoked@example.com", "sub": "one", "refresh_token": "revoked-refresh", "access_token": "expired-access"}
        path = transition(tmp_path, payload, "verified")
        checkpoint = tmp_path / "checkpoint.json"
        checkpoint.write_text(json.dumps({str(path.resolve()): {
            "fingerprint": cpa.fingerprint(payload),
            "push_fingerprint": push_fingerprint(payload),
            "pushed": False,
        }}), encoding="utf-8")
        spec = importlib.util.spec_from_file_location("cpa_reprobe_revoked_test", CLIENT / "cpa_reprobe.py")
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        monkeypatch.setattr(cpa, "probe_auth", lambda *args, **kwargs: {"decision": "refresh", "code": "ACCESS_EXPIRED", "status": 401})
        monkeypatch.setattr(cpa, "try_refresh_access_token", lambda *args, **kwargs: {"ok": False, "code": "TOKEN_INVALID"})
        stats = module.run(Namespace(
            root=str(tmp_path), checkpoint=str(checkpoint), proxy="", timeout=1,
            remote_base="https://bridge.example", remote_secret="secret",
            push_proxy="", insecure=False, push_timeout=1,
            workers=1, include_verified=True,
        ))
        assert stats["revoked_preserved"] == 1
        assert path.exists()
    finally:
        sys.path.remove(str(CLIENT))


def test_revoked_pending_auth_is_preserved(tmp_path, monkeypatch):
    sys.path.insert(0, str(CLIENT))
    try:
        import cpa
        from cpa.state import transition
        payload = {"email": "pending-revoked@example.com", "sub": "one", "refresh_token": "revoked", "access_token": "expired"}
        path = transition(tmp_path, payload, "pending")
        spec = importlib.util.spec_from_file_location("cpa_reprobe_pending_revoked_test", CLIENT / "cpa_reprobe.py")
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        monkeypatch.setattr(cpa, "probe_auth", lambda *args, **kwargs: {"decision": "refresh", "code": "ACCESS_EXPIRED"})
        monkeypatch.setattr(cpa, "try_refresh_access_token", lambda *args, **kwargs: {"ok": False, "code": "TOKEN_INVALID"})
        stats = module.run(Namespace(
            root=str(tmp_path), checkpoint="", proxy="", timeout=1,
            remote_base="", remote_secret="", push_proxy="", insecure=False,
            push_timeout=1, workers=1, include_verified=False,
        ))
        assert stats["revoked_preserved"] == 1
        assert path.exists()
    finally:
        sys.path.remove(str(CLIENT))


def test_stale_auth_push_is_terminal(tmp_path, monkeypatch):
    sys.path.insert(0, str(CLIENT))
    try:
        import cpa
        from cpa.state import transition
        payload = {"email": "stale@example.com", "sub": "one", "refresh_token": "refresh", "access_token": "access"}
        transition(tmp_path, payload, "pending")
        spec = importlib.util.spec_from_file_location("cpa_reprobe_stale_test", CLIENT / "cpa_reprobe.py")
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        monkeypatch.setattr(cpa, "probe_auth", lambda *args, **kwargs: {"decision": "pass", "code": "PROBE_PASSED"})
        pushes = {"count": 0}

        def push(**kwargs):
            pushes["count"] += 1
            return False, 422, json.dumps({"error_code": "STALE_AUTH"})

        monkeypatch.setattr(cpa, "push_auth_file", push)
        common = dict(
            root=str(tmp_path), checkpoint="", proxy="", timeout=1,
            remote_base="https://bridge.example", remote_secret="secret",
            push_proxy="", insecure=False, push_timeout=1, workers=1,
        )
        first = module.run(Namespace(**common, include_verified=False))
        second = module.run(Namespace(**common, include_verified=True))
        assert first["stale_rejected"] == 1
        assert second["terminal_skipped"] == 1
        assert pushes["count"] == 1
    finally:
        sys.path.remove(str(CLIENT))


def test_transition_refuses_to_overwrite_newer_state(tmp_path):
    sys.path.insert(0, str(CLIENT))
    try:
        from cpa.state import fingerprint, transition
        newer = {"email": "race@example.com", "sub": "one", "refresh_token": "new", "access_token": "new-access"}
        verified = transition(tmp_path, newer, "verified")
        older = dict(newer, refresh_token="old", access_token="old-access")
        pending = tmp_path / "cpa_pending" / verified.name
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text(json.dumps(older), encoding="utf-8")
        os.utime(pending, (1, 1))
        with pytest.raises(RuntimeError, match="newer auth state exists"):
            transition(
                tmp_path,
                older,
                "verified",
                source_path=pending,
                expected_source_fingerprint=fingerprint(older),
            )
        assert json.loads(verified.read_text(encoding="utf-8"))["refresh_token"] == "new"
    finally:
        sys.path.remove(str(CLIENT))


def test_reprobe_deduplicates_identity_across_state_directories(tmp_path, monkeypatch):
    sys.path.insert(0, str(CLIENT))
    try:
        import cpa
        from cpa.state import transition

        older = {"email": "duplicate@example.com", "sub": "one", "refresh_token": "old", "access_token": "old"}
        older_path = transition(tmp_path, older, "pending")
        newer = dict(older, refresh_token="new", access_token="new")
        newer_path = tmp_path / "cpa_cooldown" / older_path.name
        newer_path.parent.mkdir(parents=True, exist_ok=True)
        newer_path.write_text(json.dumps(newer), encoding="utf-8")
        os.utime(older_path, (1, 1))

        spec = importlib.util.spec_from_file_location("cpa_reprobe_identity_test", CLIENT / "cpa_reprobe.py")
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        seen = []
        monkeypatch.setattr(cpa, "probe_auth", lambda auth, **kwargs: (
            seen.append(auth["refresh_token"]) or {"decision": "pass", "code": "PROBE_PASSED"}
        ))
        monkeypatch.setattr(cpa, "push_auth_file", lambda **kwargs: (True, 200, "{}"))

        stats = module.run(Namespace(
            root=str(tmp_path), checkpoint="", proxy="", timeout=1,
            remote_base="https://bridge.example", remote_secret="secret",
            push_proxy="", insecure=False, push_timeout=1,
            workers=4, include_verified=False,
        ))

        assert stats["scanned"] == stats["passed"] == stats["pushed"] == 1
        assert seen == ["new"]
    finally:
        sys.path.remove(str(CLIENT))
