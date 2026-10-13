"""Insight Distiller —— 把低信息密度内容压缩成高密度结论。

对外主要入口：

```python
from distiller import KnowledgeBase

with KnowledgeBase() as kb:
    res = kb.search("长期国债", domain="finance", claim_types=["fact"])
```
"""

from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"

if TYPE_CHECKING:  # pragma: no cover
    from .agent import KnowledgeBase


def __getattr__(name: str) -> Any:
    """惰性导入。

    `KnowledgeBase` 会拉入 pydantic 契约与 store 层，
    而 CLI 的多数命令用不到——放在模块顶层会拖慢每次启动。
    """
    if name == "KnowledgeBase":
        from .agent import KnowledgeBase

        return KnowledgeBase
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["KnowledgeBase", "__version__"]
