"""强制 UTF-8 的文件读写封装。

**为什么必须有这一层**：Windows 上 open() 的默认编码是系统 ANSI
（简体中文环境为 cp936/GBK）。本项目内容全是中文，任何裸 open() 都会在
Windows 上乱码或抛 UnicodeDecodeError。因此全项目禁止裸 open()，统一走这里。

写文本时固定 newline="\\n"，避免 Windows 自动把 \\n 转成 \\r\\n
导致同一份内容在两个平台产生不同的文件哈希。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ENCODING = "utf-8"


def read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding=ENCODING)


def write_text(path: str | Path, content: str, *, mkdir: bool = True) -> None:
    p = Path(path)
    if mkdir:
        p.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" 关闭平台相关的换行转换
    with open(p, "w", encoding=ENCODING, newline="\n") as f:
        f.write(content)


def read_json(path: str | Path) -> Any:
    return json.loads(read_text(path))


def write_json(path: str | Path, data: Any, *, indent: int = 2) -> None:
    # ensure_ascii=False 保留中文原文，便于人工检查
    write_text(path, json.dumps(data, ensure_ascii=False, indent=indent))
