"""Web UI（M5）。

FastAPI + Jinja2 + 原生 JS，零构建步骤（不需要 Node）。
"""

from .app import WebDeps, create_app
from .deeplink import anchor_to_seconds, deeplink, format_seconds, supports_timestamp

__all__ = [
    "WebDeps",
    "anchor_to_seconds",
    "create_app",
    "deeplink",
    "format_seconds",
    "supports_timestamp",
]
