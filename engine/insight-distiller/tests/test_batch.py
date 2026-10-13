"""批量导入的可用性：subtitle_only、进度、中断续跑。

这些行为全部来自实测教训——抓 211 期系列时，原实现让人无法判断
「还要多久」「卡住了没」「中断了怎么续」，只能绕过 CLI 自己写脚本。
"""

from __future__ import annotations

import pytest

from distiller.cli import _fmt_duration
from distiller.models import ContentType, RawContent, Segment
from distiller.pipeline import SubtitleMissing


class TestSubtitleOnly:
    """无字幕内容直接跳过，不启动本地 ASR。

    背景：一条无字幕视频要占几分钟 CPU（实测 482% 占用、4.3GB 内存），
    200 条里混几条就能把批量任务拖住，且外部完全看不出在干什么。
    """

    def _video(self, segments: list[Segment] | None = None) -> RawContent:
        return RawContent(
            url="https://b.com/v1",
            content_type=ContentType.VIDEO,
            platform="bilibili",
            title="无字幕视频",
            duration_sec=600,
            segments=segments or [],
        )

    def test_raises_subtitle_missing(self, tmp_path, monkeypatch) -> None:
        from distiller.config import Config
        from distiller.pipeline import Pipeline

        cfg = Config()
        cfg.storage.data_dir = str(tmp_path)
        with Pipeline(cfg) as pipe:
            with pytest.raises(SubtitleMissing):
                pipe._maybe_transcribe(self._video(), subtitle_only=True)

    def test_article_unaffected(self, tmp_path) -> None:
        """文章类没有转写概念，不该被这个开关影响。"""
        from distiller.config import Config
        from distiller.pipeline import Pipeline

        cfg = Config()
        cfg.storage.data_dir = str(tmp_path)
        article = RawContent(
            url="https://x.com/a", content_type=ContentType.ARTICLE,
            platform="web", title="文章",
            segments=[Segment(idx=0, text="正文")],
        )
        with Pipeline(cfg) as pipe:
            assert pipe._maybe_transcribe(article, subtitle_only=True) is article

    def test_video_with_subtitle_passes_through(self, tmp_path) -> None:
        """有字幕的视频不需要 ASR，subtitle_only 不该拦它。"""
        from distiller.config import Config
        from distiller.pipeline import Pipeline

        cfg = Config()
        cfg.storage.data_dir = str(tmp_path)
        with_sub = self._video(segments=[Segment(idx=0, text="字" * 200)])
        with Pipeline(cfg) as pipe:
            assert pipe._maybe_transcribe(with_sub, subtitle_only=True) is with_sub

    def test_is_fetch_error_subclass(self) -> None:
        """继承 FetchError 让既有错误处理仍然生效，
        但单独成类以便把「预期跳过」与「真失败」分开统计。"""
        from distiller.ingest import FetchError

        assert issubclass(SubtitleMissing, FetchError)


class TestFormatDuration:
    """批量任务动辄几十分钟，纯秒数不可读。"""

    def test_seconds(self) -> None:
        assert _fmt_duration(45) == "45 秒"

    def test_minutes(self) -> None:
        assert _fmt_duration(125) == "2 分 5 秒"

    def test_hours(self) -> None:
        assert _fmt_duration(3725) == "1 小时 2 分"

    def test_negative_clamped(self) -> None:
        """时钟回拨或计算误差不该产生「-3 秒」这种输出。"""
        assert _fmt_duration(-5) == "0 秒"

    def test_zero(self) -> None:
        assert _fmt_duration(0) == "0 秒"


class TestQaExportRange:
    """导出范围解析。默认不限量——曾默认 200，
    导出 211 题的系列会静默少 11 题，这类「看起来成功」的截断最难发现。
    """

    class _FakeDB:
        def __init__(self, n: int) -> None:
            self.n = n
            self.last_sql = ""

        class _Conn:
            def __init__(self, outer) -> None:
                self.outer = outer

            def execute(self, sql, args=()):
                self.outer.last_sql = sql
                rows = [{"id": i} for i in range(1, self.outer.n + 1)]
                if "LIMIT" in sql and args:
                    rows = rows[: args[-1]]
                return type("R", (), {"fetchall": lambda _s: rows})()

        @property
        def conn(self):
            return self._Conn(self)

    class _FakePipe:
        def __init__(self, db) -> None:
            self.db = db

    def _resolve(self, *, n=211, **kw):
        from distiller.cli import _resolve_qa_ids

        db = self._FakeDB(n)
        kw.setdefault("ids", None)
        kw.setdefault("series", None)
        kw.setdefault("author", None)
        kw.setdefault("limit", None)
        return _resolve_qa_ids(self._FakePipe(db), **kw), db

    def test_no_limit_by_default(self) -> None:
        ids, db = self._resolve(series="每天一个宝藏问题")
        assert len(ids) == 211
        assert "LIMIT" not in db.last_sql

    def test_explicit_limit_applies(self) -> None:
        ids, db = self._resolve(series="x", limit=50)
        assert len(ids) == 50
        assert "LIMIT" in db.last_sql

    def test_id_range(self) -> None:
        ids, _ = self._resolve(ids="2-13")
        assert ids == list(range(2, 14))

    def test_id_list(self) -> None:
        ids, _ = self._resolve(ids="2,5,7")
        assert ids == [2, 5, 7]

    def test_id_range_not_truncated_without_limit(self) -> None:
        ids, _ = self._resolve(ids="1-300")
        assert len(ids) == 300
