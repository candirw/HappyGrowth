"""外部二进制探测与安全的子进程调用。

跨平台要点：
1. 探测顺序：显式配置 > PATH > Python 包自带（imageio-ffmpeg）。
   自带二进制让 Windows 无需任何系统级安装。
2. 一律用列表参数调用，**绝不 shell=True**——Windows 上路径含空格会失败，
   且存在命令注入风险。
3. 捕获输出时显式指定 UTF-8 并容错，避免 Windows 下 GBK 解码崩溃。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class BinaryNotFound(RuntimeError):
    pass


def find_ffmpeg(configured: str | None = None) -> Path | None:
    """定位 ffmpeg。M1 用不到，M2 音视频转写才需要。"""
    if configured:
        p = Path(configured).expanduser()
        if p.exists():
            return p

    found = shutil.which("ffmpeg")
    if found:
        return Path(found)

    # 兜底：imageio-ffmpeg 自带各平台预编译二进制
    try:
        import imageio_ffmpeg

        return Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return None


def run(
    args: list[str | Path],
    *,
    timeout: int = 300,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """安全执行外部命令。

    errors="replace" 保证即使子进程输出非 UTF-8 字节也不会抛异常
    （Windows 上部分工具输出 GBK）。
    """
    cmd = [str(a) for a in args]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,  # 显式声明，防止后人误改
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"命令执行失败（exit={proc.returncode}）: {' '.join(cmd[:3])}...\n"
            f"{(proc.stderr or '').strip()[:500]}"
        )
    return proc
