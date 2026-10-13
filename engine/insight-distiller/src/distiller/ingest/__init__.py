"""采集层入口。导入各 adapter 以触发注册。

注册顺序无关，pick_adapter 按 priority 排序选择，
通用 web adapter 的 priority 最大，因此总是最后兜底。
"""

from .base import (
    FetchError,
    SourceAdapter,
    list_adapters,
    pick_adapter,
    register_adapter,
    split_paragraphs,
)
from .declarative import DeclarativeAdapter, build_site_adapters
from .paste import PasteAdapter

# 导入触发注册副作用
from . import bilibili, wechat, xhs, zhihu, web  # noqa: F401

__all__ = [
    "DeclarativeAdapter",
    "FetchError",
    "SourceAdapter",
    "PasteAdapter",
    "build_site_adapters",
    "list_adapters",
    "pick_adapter",
    "register_adapter",
    "split_paragraphs",
]
