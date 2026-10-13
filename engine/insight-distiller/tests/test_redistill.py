"""用库内正文重跑浓缩，不重新采集。

场景：先批量采集入库（无 LLM 时也能跑），之后配好 LLM 再补浓缩。
重新采集代价很高——平台限流、字幕接口可能失效、无字幕的还要重跑转写，
而正文早就存好了。
"""

from __future__ import annotations

import pytest

from distiller.config import Config
from distiller.ingest import FetchError
from distiller.models import ContentType, RawContent, Segment
from distiller.pipeline import Pipeline


@pytest.fixture()
def pipe(tmp_path):
    cfg = Config()
    cfg.storage.data_dir = str(tmp_path)
    with Pipeline(cfg) as p:
        yield p


def _seed(pipe, *, segments: list[Segment] | None = None) -> int:
    """写入一条只有正文、没有 digest 的内容。"""
    raw = RawContent(
        url="https://b.com/BV1",
        content_type=ContentType.VIDEO,
        platform="bilibili",
        title="234. 为什么X？【每天一个宝藏问题】",
        author="海安雨",
        duration_sec=203,
        segments=segments if segments is not None else [
            Segment(idx=0, text="正文内容" * 40)
        ],
    )
    result = pipe.process(raw, distill=False)
    return int(result.source.id)


class TestRedistill:
    def test_rebuilds_raw_from_db(self, pipe, monkeypatch) -> None:
        """核心行为：不调用 fetch，正文来自库内。"""
        sid = _seed(pipe)

        def _boom(*a, **k):
            raise AssertionError("redistill 不该重新采集")

        monkeypatch.setattr(pipe, "fetch", _boom)

        captured = {}

        def fake_process(raw, **kw):
            captured["raw"] = raw
            captured["kw"] = kw
            return "sentinel"

        monkeypatch.setattr(pipe, "process", fake_process)
        assert pipe.redistill(sid) == "sentinel"

        raw = captured["raw"]
        assert raw.url == "https://b.com/BV1"
        assert raw.title.startswith("234.")
        assert raw.author == "海安雨"
        assert raw.duration_sec == 203
        assert raw.segments

    def test_passes_force_true(self, pipe, monkeypatch) -> None:
        """库里已有 source 记录，不传 force 会被当成「已消化」直接复用。"""
        sid = _seed(pipe)
        captured = {}
        monkeypatch.setattr(
            pipe, "process", lambda raw, **kw: captured.update(kw) or "ok"
        )
        pipe.redistill(sid)
        assert captured.get("force") is True

    def test_missing_source(self, pipe) -> None:
        with pytest.raises(FetchError, match="不存在"):
            pipe.redistill(99999)

    def test_source_without_segments_gives_actionable_error(self, pipe) -> None:
        """没有正文时要指出该怎么办，而不是抛难懂的异常。"""
        raw = RawContent(
            url="https://b.com/empty",
            content_type=ContentType.ARTICLE,
            platform="web",
            title="空内容",
            segments=[],
        )
        from distiller.models import SourceStatus

        sid = pipe.db.upsert_source(raw, status=SourceStatus.FETCHED)
        with pytest.raises(FetchError, match="没有正文"):
            pipe.redistill(sid)
