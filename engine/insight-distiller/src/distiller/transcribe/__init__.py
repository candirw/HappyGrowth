"""转写层入口。

核心策略：**字幕优先**。有官方/CC/AI 字幕就直接用，跳过 ASR
——速度快约 50 倍且零成本。只有无字幕时才下载音频跑 Whisper。
"""

from __future__ import annotations

from typing import Callable

from ..compat.paths import DataPaths
from ..config import Config
from ..models import RawContent, SourceStatus
from .audio import AudioError, download_audio
from .whisper_local import ASRError, ASROptions, WhisperEngine

ProgressFn = Callable[[str], None]


class Transcriber:
    def __init__(
        self,
        config: Config,
        paths: DataPaths,
        *,
        progress: ProgressFn | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.progress = progress or (lambda _m: None)

    def needs_asr(self, raw: RawContent) -> bool:
        """判断是否需要跑 ASR。

        已有字幕片段（且不只是简介兜底）就不需要。
        """
        if not raw.segments:
            return True
        # B 站无字幕时会用简介兜底，这种情况仍需 ASR 才能拿到真实内容
        only_desc = all(s.section == "简介" for s in raw.segments)
        return only_desc

    def ensure_transcript(self, raw: RawContent) -> RawContent:
        """确保 raw 带有可用的正文片段。无字幕则下载音频并转写。"""
        tc = self.config.transcribe

        if not self.needs_asr(raw):
            return raw

        if not tc.enabled:
            raw.warnings.append(
                "该内容无字幕，且转写已在配置中关闭（transcribe.enabled=false）"
            )
            return raw

        try:
            self.progress("无字幕，开始下载音频")
            audio_path = download_audio(
                raw.url,
                self.paths.audio,
                config=self.config.ingest,
                basename=raw.title,
            )
            self.progress(f"音频就绪：{audio_path.name}")

            engine = WhisperEngine(
                ASROptions(
                    model=tc.model,
                    language=tc.language,
                    compute_type=tc.compute_type,
                    vad=tc.vad,
                    hotwords=tc.hotwords,
                ),
                progress=self.progress,
            )
            segments, lang = engine.transcribe(audio_path)
        except (AudioError, ASRError) as exc:
            raw.warnings.append(f"转写失败：{exc}")
            self.progress(f"转写失败：{exc}")
            return raw

        if not segments:
            raw.warnings.append("转写未产生任何文本（可能是纯音乐或静音）")
            return raw

        # 用转写结果替换简介兜底内容
        raw.segments = segments
        raw.lang = raw.lang or lang
        raw.audio_rel_path = self.paths.relative(audio_path)
        raw.warnings = [w for w in raw.warnings if "无可用字幕" not in w]
        raw.warnings.append(f"已通过本地 Whisper 转写（{len(segments)} 段）")

        if not self.config.storage.keep_audio:
            audio_path.unlink(missing_ok=True)
            raw.audio_rel_path = None
            self.progress("已删除音频文件（转写文本永久缓存）")

        return raw


__all__ = [
    "Transcriber",
    "ASRError",
    "AudioError",
    "ASROptions",
    "WhisperEngine",
    "download_audio",
]
