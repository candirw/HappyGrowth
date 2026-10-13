"""uvicorn --reload 的入口模块。

reload 模式要求传 import 字符串（而非 app 对象），因为子进程需要
重新导入模块。配置路径通过环境变量传递。
"""

from __future__ import annotations

import os

from ..config import load_config
from .app import create_app

app = create_app(
    load_config(os.environ.get("DISTILLER_CONFIG")),
    profile=os.environ.get("DISTILLER_PROFILE") or None,
)

__all__ = ["app"]
