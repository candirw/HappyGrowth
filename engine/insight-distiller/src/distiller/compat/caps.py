"""平台能力矩阵。

`distill doctor` 用它输出当前平台实际可用的能力，
迁移到 Windows 后第一件事就是跑 doctor 对照检查。
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field
from importlib.util import find_spec


def _has(module: str) -> bool:
    try:
        return find_spec(module) is not None
    except (ImportError, ValueError):
        return False


@dataclass
class PlatformCaps:
    os_name: str
    os_version: str
    arch: str
    python_version: str
    ocr_engines: list[str] = field(default_factory=list)
    asr_engines: list[str] = field(default_factory=list)
    has_ffmpeg: bool = False
    # 音频下载器。ASR 引擎装了但没下载器时，无字幕视频依然无法处理——
    # 只报「ASR: faster-whisper」会让人以为转写可用，
    # 结果真跑的时候才在下载环节失败。
    has_downloader: bool = False
    has_vector: bool = False
    has_paper: bool = False
    # 浏览器 cookie 读取。配了 cookies_from_browser 但缺这个包时，
    # cookie 会被静默忽略——B 站投稿列表直接返回 -352 风控错误，
    # 而用户以为自己已经配好了登录态。
    has_browser_cookie: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def can_transcribe(self) -> bool:
        """能否真的完成转写。三个条件缺一不可。"""
        return bool(self.asr_engines) and self.has_ffmpeg and self.has_downloader

    @property
    def ocr_default(self) -> str | None:
        """默认 OCR 引擎。

        优先 RapidOCR：它三平台通用，能保证 macOS 与 Windows 上
        OCR 结果一致，避免迁移后摘要质量变化这类极难排查的问题。
        ocrmac 仅作为 macOS 上的可选加速项。
        """
        for name in ("rapidocr", "ocrmac"):
            if name in self.ocr_engines:
                return name
        return None


def detect_caps() -> PlatformCaps:
    caps = PlatformCaps(
        os_name=platform.system(),
        os_version=platform.release(),
        arch=platform.machine(),
        python_version=sys.version.split()[0],
    )

    # 两个包名都要认：RapidOCR 在 2.x 改名，
    # 旧包 rapidocr-onnxruntime 仅支持 Python <3.13
    if _has("rapidocr") or _has("rapidocr_onnxruntime"):
        caps.ocr_engines.append("rapidocr")
    if sys.platform == "darwin" and _has("ocrmac"):
        caps.ocr_engines.append("ocrmac")
    if not caps.ocr_engines:
        # 注意：提示文本含方括号，展示层必须转义 rich markup，否则会被当标签吞掉
        caps.notes.append(
            ' 未安装 OCR 引擎，公众号图片型文章将无法提取正文：pip install -e ".[ocr]"'.strip()
        )

    if _has("faster_whisper"):
        caps.asr_engines.append("faster-whisper")
    if sys.platform == "darwin" and _has("mlx_whisper"):
        caps.asr_engines.append("mlx-whisper")

    from .binaries import find_ffmpeg

    caps.has_ffmpeg = find_ffmpeg() is not None
    caps.has_downloader = _has("yt_dlp")
    caps.has_vector = _has("lancedb")
    caps.has_paper = _has("pymupdf4llm")
    caps.has_browser_cookie = _has("browser_cookie3")

    if not caps.asr_engines:
        caps.notes.append(
            '未安装 ASR，无字幕视频暂不可转写（M2 功能）：pip install -e ".[av]"'
        )
    elif not caps.can_transcribe:
        # ASR 装了但链路不完整。必须明确说缺哪一环——
        # 否则用户看到「ASR: faster-whisper」会以为能用，
        # 真跑到无字幕视频时才在下载或解码环节失败。
        missing = []
        if not caps.has_downloader:
            missing.append("yt-dlp（音频下载）")
        if not caps.has_ffmpeg:
            missing.append("ffmpeg（音频解码）")
        caps.notes.append(
            f"ASR 引擎已装但链路不完整，缺 {'、'.join(missing)}："
            'pip install -e ".[av]"'
        )

    return caps
