"""采集层抽象与注册表。

新增一个平台 = 新增一个文件 + @register_adapter 装饰器，不需要改 pipeline。
这是可迭代性的核心机制。
"""

from __future__ import annotations

import re
import time
from typing import Callable

import httpx

from ..config import IngestConfig
from ..models import RawContent


class FetchError(RuntimeError):
    """采集失败。上层会提示用户改用 --paste 手动粘贴兜底，不中断整体流程。"""


class SourceAdapter:
    """所有采集器的统一接口。"""

    name: str = ""
    # 越小越优先，通用网页兜底器用大值
    priority: int = 100

    def __init__(self, config: IngestConfig) -> None:
        # 存平台级合并后的配置：采集器内部不必再判断「有没有覆盖」
        self.config = config.effective(self.name) if self.name else config
        self._global = config

    def match(self, url: str) -> bool:
        raise NotImplementedError

    def fetch(self, url: str) -> RawContent:
        raise NotImplementedError

    # ---------- 共用工具 ----------

    def _extra_headers(self) -> dict[str, str]:
        return dict(self._global.platform(self.name).headers) if self.name else {}

    def _throttle(self) -> None:
        """同平台连续请求的最小间隔。

        风控严的平台（小红书、抖音）必须限流——短时间连续请求
        是触发验证码最直接的原因，而一旦触发，后续几分钟内全部失败。
        """
        gap = self._global.platform(self.name).rate_limit_sec if self.name else 0.0
        if gap <= 0:
            return
        last = _LAST_REQUEST.get(self.name, 0.0)
        wait = gap - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST[self.name] = time.monotonic()

    def _client(self, **kwargs) -> httpx.Client:
        headers = {"User-Agent": self.config.user_agent}
        headers.update(self._extra_headers())
        headers.update(kwargs.pop("headers", {}))
        return httpx.Client(
            headers=headers,
            timeout=self.config.timeout,
            follow_redirects=True,
            proxy=self.config.proxy,
            **kwargs,
        )

    def _get(self, url: str, **kwargs) -> httpx.Response:
        last_exc: Exception | None = None
        for _ in range(max(1, self.config.retry + 1)):
            self._throttle()
            try:
                with self._client() as client:
                    resp = client.get(url, **kwargs)
                if resp.status_code == 200:
                    return resp
                last_exc = FetchError(f"HTTP {resp.status_code}")
            except httpx.RequestError as exc:
                last_exc = exc
        raise FetchError(f"请求失败: {url} ({last_exc})")


_ADAPTERS: list[type[SourceAdapter]] = []
# 各平台最近一次请求时间，用于限流
_LAST_REQUEST: dict[str, float] = {}

# 通用网页兜底器的优先级。它对所有 URL 都 match，
# 因此「是不是专用采集器」就以此为界。
FALLBACK_PRIORITY = 100


def register_adapter(name: str, *, priority: int = 100) -> Callable[[type[SourceAdapter]], type[SourceAdapter]]:
    def deco(cls: type[SourceAdapter]) -> type[SourceAdapter]:
        cls.name = name
        cls.priority = priority
        _ADAPTERS.append(cls)
        return cls

    return deco


def pick_adapter(url: str, config: IngestConfig) -> SourceAdapter:
    """按优先级选择匹配的采集器。

    三点设计：
    ① 声明式站点规则优先于通用兜底，但不覆盖内置专用采集器——
       用户为 bilibili 写选择器是没意义的（字幕逻辑无法用选择器表达）。
    ② **专用采集器被禁用时明确报错，不降级到通用抽取。**
       通用兜底器对所有 URL 都 match，不特殊处理的话禁用会变成静默降级；
       而小红书正文在嵌套 JSON 里、B 站正文在字幕接口里，
       通用抽取只会拿到空内容或噪声——用户却不知道为什么。
    ③ 通用兜底器保证总能返回一个。
    """
    from .declarative import build_site_adapters

    candidates: list[SourceAdapter] = [cls(config) for cls in _ADAPTERS]
    candidates.extend(build_site_adapters(config))

    for adapter in sorted(candidates, key=lambda a: a.priority):
        if not adapter.match(url):
            continue
        if config.is_enabled(adapter.name):
            return adapter
        # 专用采集器（priority < FALLBACK_PRIORITY）被禁用则直接失败，
        # 不让通用兜底悄悄接管
        if adapter.priority < FALLBACK_PRIORITY:
            raise FetchError(
                f"该链接对应的采集器「{adapter.name}」已在配置中禁用。\n"
                f"  如需启用：在 config.yaml 设置 "
                f"ingest.platforms.{adapter.name}.enabled: true\n"
                f"  或用 distill add \"标题\" --paste 手动粘贴正文。"
            )

    raise FetchError(
        f"没有可用的采集器处理: {url}\n"
        f"  （通用网页采集器可能也已被禁用）"
    )


def list_adapters(config: IngestConfig | None = None) -> list[str]:
    """列出采集器名。传入配置时会附带声明式站点并标注禁用状态。"""
    names = [c.name for c in sorted(_ADAPTERS, key=lambda c: c.priority)]
    if config is None:
        return names

    from .declarative import build_site_adapters

    names.extend(a.name for a in build_site_adapters(config))
    return [n if config.is_enabled(n) else f"{n}(已禁用)" for n in names]


_WS = re.compile(r"[ \t\u3000]+")


def split_paragraphs(text: str, *, min_len: int = 1) -> list[str]:
    """按空行/换行切段并清理空白。用于文章型内容生成 segments。"""
    parts: list[str] = []
    for block in re.split(r"\n\s*\n|\n", text or ""):
        cleaned = _WS.sub(" ", block).strip()
        if len(cleaned) >= min_len:
            parts.append(cleaned)
    return parts
