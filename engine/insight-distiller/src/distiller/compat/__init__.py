"""跨平台适配层。

本包是整个项目**唯一**允许出现平台判断（sys.platform / platform.system）的地方。
业务代码一律通过这里的封装访问文件系统、外部二进制与计算设备，
迁移到 Windows 时只需检查本目录。

命名说明：不用 `platform` 作为包名，以免与标准库 `platform` 模块混淆。
"""

from .paths import (
    DataPaths,
    safe_filename,
    get_data_paths,
)
from .fsio import read_text, write_text, read_json, write_json
from .caps import PlatformCaps, detect_caps

__all__ = [
    "DataPaths",
    "safe_filename",
    "get_data_paths",
    "read_text",
    "write_text",
    "read_json",
    "write_json",
    "PlatformCaps",
    "detect_caps",
]
