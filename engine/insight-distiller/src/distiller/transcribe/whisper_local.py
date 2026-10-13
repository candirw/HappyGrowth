"""本地 ASR（faster-whisper）。

选型理由（见 PORTABILITY.md）：faster-whisper 基于 CTranslate2，**三平台通用**，
作为基准实现；mlx-whisper 仅 Apple Silicon 可用，只能是可选加速。

两个关键实践：
1. **VAD 必开**：切掉静音既省算力，又能避免 Whisper 在静音段产生幻觉重复。
2. **hotwords 注入术语表**：显著降低专有名词错字率（中文技术内容尤其明显）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from ..compat.device import detect_device
from ..models import Segment

ProgressFn = Callable[[str], None]


class ASRError(RuntimeError):
    pass


@dataclass
class ASROptions:
    model: str = "large-v3"
    language: str | None = None       # None = 自动检测，中英混说友好
    compute_type: str = "auto"        # auto 按平台探测
    device: str = "auto"
    vad: bool = True
    hotwords: list[str] = field(default_factory=list)
    beam_size: int = 5


class WhisperEngine:
    name = "faster-whisper"

    def __init__(self, options: ASROptions, *, progress: ProgressFn | None = None) -> None:
        self.options = options
        self.progress = progress or (lambda _m: None)
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ASRError('未安装 faster-whisper。请执行：pip install -e ".[av]"') from exc

        info = detect_device()
        device = self.options.device if self.options.device != "auto" else info.device
        compute = (
            self.options.compute_type
            if self.options.compute_type != "auto"
            else info.compute_type
        )
        # CTranslate2 目前不支持 mps，Apple Silicon 上走 CPU（性能仍可接受）
        if device == "mps":
            device, compute = "cpu", "int8"

        self.progress(f"加载 Whisper 模型 {self.options.model}（{device}/{compute}）")
        try:
            self._model = WhisperModel(
                self.options.model, device=device, compute_type=compute
            )
        except Exception as exc:  # noqa: BLE001
            raise ASRError(f"Whisper 模型加载失败: {exc}") from exc
        return self._model

    def transcribe(self, audio_path: Path) -> tuple[list[Segment], str | None]:
        """转写音频，返回带时间戳的片段与检测到的语言。"""
        model = self._load()

        kwargs: dict = {
            "beam_size": self.options.beam_size,
            "vad_filter": self.options.vad,
        }
        if self.options.language:
            kwargs["language"] = self.options.language
        if self.options.hotwords:
            # 注入术语表，降低专有名词错字
            kwargs["hotwords"] = " ".join(self.options.hotwords)

        self.progress("开始转写（首次运行需下载模型，请耐心等待）")
        try:
            raw_segments, info = model.transcribe(str(audio_path), **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise ASRError(f"转写失败: {exc}") from exc

        segments = list(self._collect(raw_segments))
        lang = getattr(info, "language", None)
        self.progress(f"转写完成：{len(segments)} 段 · 语言 {lang}")
        return segments, lang

    def _collect(self, raw_segments: Iterator) -> Iterator[Segment]:
        """流式消费 whisper 输出并合并成语义段。

        Whisper 单段通常很短，直接送 LLM 会浪费大量 token 在碎片上，
        因此按字数合并（与 B 站字幕处理一致）。
        """
        buf: list[tuple[str, int, int]] = []
        idx = 0
        target_chars = 120

        for seg in raw_segments:
            text = (seg.text or "").strip()
            if not text:
                continue
            buf.append((text, int(seg.start * 1000), int(seg.end * 1000)))
            if sum(len(t) for t, _, _ in buf) >= target_chars:
                yield Segment(
                    idx=idx,
                    text="".join(t for t, _, _ in buf),
                    start_ms=buf[0][1],
                    end_ms=buf[-1][2],
                )
                idx += 1
                buf.clear()

        if buf:
            yield Segment(
                idx=idx,
                text="".join(t for t, _, _ in buf),
                start_ms=buf[0][1],
                end_ms=buf[-1][2],
            )
