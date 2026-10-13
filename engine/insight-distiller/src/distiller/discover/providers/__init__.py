"""发现器集合。导入各实现以触发注册。"""

from .base import (
    DiscoveryError,
    DiscoveryProvider,
    RateLimiter,
    get_provider,
    list_providers,
    register_provider,
)
from . import bilibili  # noqa: F401 - 触发注册副作用

__all__ = [
    "DiscoveryError",
    "DiscoveryProvider",
    "RateLimiter",
    "get_provider",
    "list_providers",
    "register_provider",
]
