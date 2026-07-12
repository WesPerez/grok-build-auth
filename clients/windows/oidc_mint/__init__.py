"""OIDC 设备码铸造包：为免费 Grok Build 账号铸造 access/refresh/id token。

仅保留铸造所需的三块：
  - oauth_device：xAI 设备码授权 + 轮询 token
  - browser_confirm：独立 Chromium 完成设备码确认
  - proxyutil：代理解析

输出成什么格式（CPA / 其它）由上层的 cpa_export 负责，这里不掺和。
"""

from .browser_confirm import mint_with_browser, shutdown_mint_browsers
from .oauth_device import CLIENT_ID, SCOPE, OAuthDeviceError
from .proxyutil import resolve_proxy, set_runtime_proxy

__all__ = [
    "mint_with_browser",
    "shutdown_mint_browsers",
    "CLIENT_ID",
    "SCOPE",
    "OAuthDeviceError",
    "resolve_proxy",
    "set_runtime_proxy",
]
