"""发现器抽象与注册表。

与采集层同样的插件机制：新增平台 = 新增一个文件 + 装饰器。
"""

from __future__ import annotations

import time
from typing import Callable

from ...config import IngestConfig
from ..models import Candidate, Platform, Query


class DiscoveryError(RuntimeError):
    """发现失败。上层会提示改用直接给 URL 的方式，不中断整体流程。"""


class RateLimiter:
    """简单令牌桶限流。

    平台反爬对请求频率敏感，尤其是需要逐条补齐元数据时
    （几百次请求很容易触发风控）。宁慢勿封。
    """

    def __init__(self, min_interval: float = 0.35) -> None:
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        gap = now - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.monotonic()


class DiscoveryProvider:
    """平台发现器统一接口。"""

    platform: Platform = Platform.UNKNOWN

    def __init__(self, config: IngestConfig) -> None:
        self.config = config
        self.limiter = RateLimiter()

    def resolve(self, name: str) -> list[tuple[str, str, str]]:
        """实体消歧：名称 → [(id, 显示名, 补充说明)]。

        返回多个候选时由上层反问用户选择。
        """
        raise NotImplementedError

    def discover(self, query: Query) -> list[Candidate]:
        """按查询拉取候选内容。"""
        raise NotImplementedError

    def supports_metric(self, metric: str) -> bool:
        """是否原生支持按该指标排序。不支持则由 ranker 本地补偿。"""
        return False


_PROVIDERS: dict[Platform, type[DiscoveryProvider]] = {}


def register_provider(
    platform: Platform,
) -> Callable[[type[DiscoveryProvider]], type[DiscoveryProvider]]:
    def deco(cls: type[DiscoveryProvider]) -> type[DiscoveryProvider]:
        cls.platform = platform
        _PROVIDERS[platform] = cls
        return cls

    return deco


def get_provider(platform: Platform, config: IngestConfig) -> DiscoveryProvider:
    cls = _PROVIDERS.get(platform)
    if cls is None:
        raise DiscoveryError(
            f"暂不支持在 {platform.value} 上做批量发现。"
            f"已支持：{', '.join(p.value for p in _PROVIDERS)}。"
            f"你可以直接提供内容链接用 `distill add` 逐条消化。"
        )
    return cls(config)


def list_providers() -> list[str]:
    return [p.value for p in _PROVIDERS]
