import importlib.util
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).resolve().parents[1] / "run.py"
SPEC = importlib.util.spec_from_file_location("grok_run_concurrency_test", MODULE_PATH)
assert SPEC and SPEC.loader
RUN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUN)


class FakeClient:
    def __init__(self, **kwargs):
        self.proxy = kwargs.get("proxy")

    def visit_home(self): pass
    def load_signup_page(self): pass
    def create_email_validation_code(self, email): return SimpleNamespace(ok=True)
    def verify_email_validation_code(self, email, code): return SimpleNamespace(ok=True)
    def require_grpc_success(self, name, result): return result
    def validate_password(self, email, password): return None
    def create_account(self, **kwargs): return SimpleNamespace(ok=True, http_status=200)
    def fetch_sso_token(self, **kwargs): return "sso"
    def close(self): pass


class FakeReceiver:
    def wait_for_code(self, timeout): return "ABC123"
    def close(self): pass


class ForbiddenLock:
    def __enter__(self):
        raise AssertionError("protocol OAuth must not acquire the browser lock")

    def __exit__(self, *args): return False


def test_protocol_registration_passes_sticky_proxy_without_browser_lock(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    created = {}

    def make_client(**kwargs):
        client = FakeClient(**kwargs)
        created["client"] = client
        return client

    def complete_oauth(*args, **kwargs):
        created["oauth_proxy"] = kwargs["proxy"]
        return SimpleNamespace(cliproxyapi_path=auth)

    monkeypatch.setattr(RUN, "YESCAPTCHA_KEY", "key")
    monkeypatch.setattr(RUN, "XConsoleAuthClient", make_client)
    monkeypatch.setattr(RUN, "_make_email_provider", lambda backend: ("xaiabcdef@example.com", FakeReceiver()))
    monkeypatch.setattr(
        RUN,
        "YesCaptchaProvider",
        lambda key: SimpleNamespace(
            solve_turnstile=lambda challenge: SimpleNamespace(token="token")
        ),
    )
    monkeypatch.setattr(RUN, "extract_cookies_from_auth_client", lambda client: {"sso": "cookie"})
    monkeypatch.setattr(RUN, "complete_build_oauth", complete_oauth)
    monkeypatch.setattr(RUN, "_oauth_lock", ForbiddenLock())
    RUN._total = 1
    RUN._t0 = 0

    proxy = "http://user:password@proxy.example:8080"
    result = RUN.register_one(
        1,
        email_backend="imap",
        cliproxyapi_auth_dir=tmp_path,
        oauth_protocol=True,
        proxy_url=proxy,
    )

    assert result["error"] is None
    assert created["client"].proxy == proxy
    assert created["oauth_proxy"] == proxy
