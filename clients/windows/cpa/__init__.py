"""Sub2API xAI auth 组装、写盘与 bridge 推送（保留 cpa 兼容接口）。"""

from .client import CpaPushError, push_auth_file
from .preprobe import probe_auth, try_refresh_access_token
from .schema import (
    API_BASE_URL,
    CLI_BASE_URL,
    CLIENT_ID,
    DEFAULT_BASE_URL,
    REDIRECT_URI,
    TOKEN_ENDPOINT,
    build_cpa_xai_auth,
    credential_file_name,
    expired_from_access_token,
)
from .writer import write_cpa_xai_auth
from .state import (
    delete_if_fingerprint,
    fingerprint,
    identity,
    operation_lock,
    push_fingerprint,
    replace_if_fingerprint,
    transition,
)

__all__ = [
    "API_BASE_URL",
    "CLI_BASE_URL",
    "CLIENT_ID",
    "DEFAULT_BASE_URL",
    "REDIRECT_URI",
    "TOKEN_ENDPOINT",
    "CpaPushError",
    "build_cpa_xai_auth",
    "credential_file_name",
    "expired_from_access_token",
    "probe_auth",
    "push_auth_file",
    "try_refresh_access_token",
    "write_cpa_xai_auth",
    "transition",
    "fingerprint",
    "identity",
    "operation_lock",
    "push_fingerprint",
    "replace_if_fingerprint",
    "delete_if_fingerprint",
]
