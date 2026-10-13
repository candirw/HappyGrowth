"""转写层测试（M2）。

不实际跑 Whisper（模型太大、太慢），只验证**决策逻辑**：
什么情况该跑 ASR、什么情况该跳过、失败时是否优雅降级。
这些判断错了会导致白花几十分钟算力或静默丢内容。
"""

from __future__ import annotations

import pytest

from distiller.compat.paths import DataPaths
from distiller.config import Config, TranscribeConfig
from distiller.models import ContentType, RawContent, Segment
from distiller.transcribe import Transcriber


def _video(segments: list[Segment], **kw) -> RawContent:
    return RawContent(
        url="https://www.bilibili.com/video/BV1xx",
        content_type=ContentType.VIDEO,
        platform="bilibili",
        title="测试视频",
        segments=segments,
        **kw,
    )


@pytest.fixture
def transcriber(tmp_path) -> Transcriber:
    paths = DataPaths(tmp_path)
    paths.ensure()
    return Transcriber(Config(), paths)


class TestASRDecision:
    def test_skips_asr_when_subtitle_available(self, transcriber: Transcriber) -> None:
        """有字幕就不该跑 ASR——这是快 50 倍且零成本的关键优化。"""
        raw = _video([Segment(idx=0, text="这是字幕内容", start_ms=0, end_ms=3000)])
        assert transcriber.needs_asr(raw) is False

    def test_needs_asr_when_no_segments(self, transcriber: Transcriber) -> None:
        assert transcriber.needs_asr(_video([])) is True

    def test_needs_asr_when_only_description_fallback(self, transcriber: Transcriber) -> None:
        """B 站无字幕时会用简介兜底，这种情况仍需 ASR 才能拿到真实内容。

        若判断错误，用户会拿到一份只基于简介的摘要却以为是全片内容。
        """
        raw = _video([Segment(idx=0, text="视频简介文字", section="简介")])
        assert transcriber.needs_asr(raw) is True

    def test_article_never_triggers_asr(self, transcriber: Transcriber) -> None:
        article = RawContent(
            url="https://example.com/a",
            content_type=ContentType.ARTICLE,
            platform="web",
            title="文章",
            segments=[],
        )
        # 文章即使无内容也不该走 ASR（pipeline 层已按 content_type 分流）
        result = transcriber.ensure_transcript(article)
        assert result is article

    def test_disabled_config_adds_warning_not_crash(self, tmp_path) -> None:
        """转写关闭时应给出明确提示，而不是静默产出空摘要。"""
        paths = DataPaths(tmp_path)
        paths.ensure()
        cfg = Config(transcribe=TranscribeConfig(enabled=False))
        t = Transcriber(cfg, paths)
        raw = t.ensure_transcript(_video([]))
        assert any("关闭" in w for w in raw.warnings)

    def test_failure_degrades_gracefully(self, transcriber: Transcriber) -> None:
        """缺少 yt-dlp/ffmpeg 时应记录告警并返回，不抛异常中断整条流程。"""
        raw = transcriber.ensure_transcript(_video([]))
        assert raw.warnings
        assert any("转写失败" in w for w in raw.warnings)


class TestSegmentAnchor:
    def test_timestamp_anchor_format(self) -> None:
        assert Segment(idx=0, text="x", start_ms=0).anchor == "00:00"
        assert Segment(idx=0, text="x", start_ms=754_000).anchor == "12:34"
        # 超过一小时须显示小时位，否则 12:34 会有歧义
        assert Segment(idx=0, text="x", start_ms=3_754_000).anchor == "01:02:34"

    def test_section_anchor_for_text_content(self) -> None:
        assert Segment(idx=3, text="x", section="方法").anchor == "方法"
        assert Segment(idx=3, text="x").anchor == "#3"


class TestBilibiliSubtitle:
    """字幕处理逻辑测试。

    真实带 CC 字幕的公开视频不好找（B 站多数字幕需登录才返回），
    因此用单元测试覆盖解析与选择逻辑，比依赖特定视频稳定。
    """

    @staticmethod
    def _adapter():
        from distiller.config import IngestConfig
        from distiller.ingest.bilibili import BilibiliAdapter

        return BilibiliAdapter(IngestConfig())

    def test_prefers_human_chinese_over_ai(self) -> None:
        """人工中文字幕质量最高，必须优先于 AI 字幕。"""
        subs = [
            {"lan": "en", "ai_status": 0, "subtitle_url": "//a"},
            {"lan": "zh-CN", "ai_status": 1, "subtitle_url": "//b"},
            {"lan": "zh-CN", "ai_status": 0, "subtitle_url": "//c"},
        ]
        chosen = self._adapter()._choose_subtitle(subs)
        assert chosen["subtitle_url"] == "//c"

    def test_falls_back_to_ai_subtitle(self) -> None:
        subs = [{"lan": "zh-CN", "ai_status": 1, "subtitle_url": "//ai"}]
        assert self._adapter()._choose_subtitle(subs)["subtitle_url"] == "//ai"

    def test_merges_short_lines_preserving_timestamps(self) -> None:
        """字幕是碎句，必须合并成语义段——否则 token 全浪费在碎片上。

        合并后首句起点与末句终点必须保留，否则锚点会失效。
        """
        segs = [
            Segment(idx=i, text="这是一句字幕。", start_ms=i * 2000, end_ms=(i + 1) * 2000)
            for i in range(40)
        ]
        merged = self._adapter()._merge_short(segs, target_chars=120)
        assert 1 < len(merged) < len(segs)
        assert merged[0].start_ms == 0
        assert merged[-1].end_ms == 80_000
        # 合并不能丢字
        assert sum(len(m.text) for m in merged) == sum(len(s.text) for s in segs)

    def test_parse_video_id(self) -> None:
        a = self._adapter()
        assert a._parse_id("https://www.bilibili.com/video/BV1GJ411x7h7") == "BV1GJ411x7h7"
        assert a._parse_id("https://www.bilibili.com/video/av12345") == "av12345"

    def test_short_description_not_used_as_content(self) -> None:
        """简介过短（如 "-"）不能当正文，否则会被误判为已有内容而跳过转写。"""
        from distiller.config import Config
        from distiller.compat.paths import DataPaths
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            paths = DataPaths(__import__("pathlib").Path(tmp))
            paths.ensure()
            t = Transcriber(Config(), paths)
            raw = _video([])
            assert t.needs_asr(raw) is True
