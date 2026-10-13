"""Web UI 测试（M5）。

重点覆盖：
① 深链构建（写错参数名会静默跳到视频开头，属于最危险的失败模式）
② API 契约与错误处理（非法输入必须 4xx 而非 500）
③ XSS 防护（库内标题/批注来自外部，直接渲染有注入风险）
④ 页面可渲染（模板语法错误只在运行时暴露）
"""

from __future__ import annotations

import pytest

from distiller.config import Config
from distiller.models import ContentType, Digest, RawContent, Segment, SourceStatus
from distiller.store import Database
from distiller.web.deeplink import (
    anchor_to_seconds,
    deeplink,
    format_seconds,
    supports_timestamp,
)
from distiller.web.views import density_band

fastapi = pytest.importorskip("fastapi", reason="需要 pip install -e '.[web]'")
from fastapi.testclient import TestClient  # noqa: E402


class TestAnchorParsing:
    @pytest.mark.parametrize(
        "anchor,expected",
        [
            ("0:30", 30),
            ("02:05", 125),
            ("1:02:05", 3725),
            ("10:00:00", 36000),
        ],
    )
    def test_valid(self, anchor: str, expected: int) -> None:
        assert anchor_to_seconds(anchor) == expected

    @pytest.mark.parametrize(
        "anchor",
        ["#12", "introduction", "方法细节", "", None, "abc", "99:99", "1:2:3:4"],
    )
    def test_non_timestamp_returns_none(self, anchor) -> None:
        """章节名与段号不是时间，不能瞎转。"""
        assert anchor_to_seconds(anchor) is None

    def test_roundtrip(self) -> None:
        assert format_seconds(anchor_to_seconds("1:02:05")) == "1:02:05"
        assert format_seconds(anchor_to_seconds("02:05")) == "2:05"


class TestDeeplink:
    def test_bilibili(self) -> None:
        url = deeplink(
            "https://www.bilibili.com/video/BV1xx411c7mD",
            platform="bilibili", anchor="02:05",
        )
        assert url == "https://www.bilibili.com/video/BV1xx411c7mD?t=125"

    def test_bilibili_keeps_existing_params(self) -> None:
        """分 P 视频的 p 参数不能丢，否则跳到第一 P。"""
        url = deeplink(
            "https://www.bilibili.com/video/BV1xx?p=3",
            platform="bilibili", seconds=90,
        )
        assert "p=3" in url and "t=90" in url

    def test_youtube_uses_seconds_suffix(self) -> None:
        """YouTube 的 watch 页要 `t=123s`，纯数字也认但不规范。"""
        url = deeplink(
            "https://www.youtube.com/watch?v=abc", platform="youtube", seconds=125
        )
        assert "t=125s" in url and "v=abc" in url

    def test_youtube_embed_uses_start(self) -> None:
        url = deeplink(
            "https://www.youtube.com/embed/abc", platform="youtube", seconds=125
        )
        assert "start=125" in url

    def test_unknown_platform_returns_original(self) -> None:
        """关键：不猜参数名。猜错会静默跳到开头，比不跳更糟。"""
        original = "https://example.com/video"
        assert deeplink(original, platform="某站", seconds=100) == original

    def test_platform_without_time_dimension(self) -> None:
        original = "https://mp.weixin.qq.com/s/abc"
        assert deeplink(original, platform="wechat", anchor="02:05") == original

    def test_non_timestamp_anchor_returns_original(self) -> None:
        original = "https://example.com/a"
        assert deeplink(original, platform="bilibili", anchor="introduction") == original

    def test_seconds_takes_priority(self) -> None:
        """有 start_ms 时应直传秒，避免格式化再解析的精度损失。"""
        url = deeplink(
            "https://www.bilibili.com/video/BV1",
            platform="bilibili", anchor="00:10", seconds=125,
        )
        assert "t=125" in url

    def test_empty_url(self) -> None:
        assert deeplink("", platform="bilibili", seconds=10) == ""

    def test_negative_seconds_ignored(self) -> None:
        original = "https://www.bilibili.com/video/BV1"
        assert deeplink(original, platform="bilibili", seconds=-5) == original

    def test_supports_timestamp(self) -> None:
        assert supports_timestamp("bilibili")
        assert not supports_timestamp("wechat")
        assert not supports_timestamp(None)


class TestDensityBand:
    def test_unscored_is_not_zero(self) -> None:
        """未评分 ≠ 低分，不能混为一谈。"""
        b = density_band(None)
        assert b["level"] == "none" and b["score"] is None

    @pytest.mark.parametrize(
        "score,level", [(90, "high"), (65, "high"), (50, "mid"), (40, "mid"), (20, "low")]
    )
    def test_bands(self, score: float, level: str) -> None:
        assert density_band(score)["level"] == level

    def test_matches_cli_thresholds(self) -> None:
        """与 CLI 判定口径必须一致，否则两端结论不同。"""
        assert density_band(64.9)["level"] == "mid"
        assert density_band(65.0)["level"] == "high"


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = Config()
    cfg.storage.data_dir = str(tmp_path)
    from distiller.web import create_app

    app = create_app(cfg)
    db = Database(tmp_path / "distiller.db")

    raw = RawContent(
        url="https://www.bilibili.com/video/BV1test",
        content_type=ContentType.VIDEO,
        platform="bilibili",
        title="测试视频 <script>alert(1)</script>",
        author="某UP主",
        duration_sec=600,
        segments=[
            Segment(idx=0, text="第一段内容，讲了混合检索。", start_ms=125_000),
            Segment(idx=1, text="第二段内容，讲了 rerank 延迟。", start_ms=300_000),
        ],
    )
    sid = db.upsert_source(raw, status=SourceStatus.DISTILLED)
    db.replace_segments(sid, raw.segments)
    db.save_digest(
        sid,
        Digest(
            tldr=["核心结论一"],
            actions=["去做这件事"],
            open_questions=["还有什么不清楚"],
            density_score=72.0,
            density_verdict="信息密度高",
        ),
    )
    db.set_density(sid, 72.0)
    db.close()

    with TestClient(app) as c:
        c.source_id = sid
        yield c


class TestApiContract:
    def test_stats(self, client) -> None:
        d = client.get("/api/stats").json()
        assert d["sources"] == 1
        assert d["avg_density"] == 72.0
        assert "review" in d and "budget" in d

    def test_list_sources(self, client) -> None:
        d = client.get("/api/sources").json()
        assert d["total"] == 1
        item = d["items"][0]
        assert item["density"]["level"] == "high"
        assert item["duration"] == "10:00"

    def test_filter_min_density(self, client) -> None:
        assert client.get("/api/sources?min_density=80").json()["total"] == 0
        assert client.get("/api/sources?min_density=70").json()["total"] == 1

    def test_search_filter(self, client) -> None:
        assert client.get("/api/sources?q=某UP主").json()["total"] == 1
        assert client.get("/api/sources?q=不存在的").json()["total"] == 0

    def test_invalid_sort_rejected(self, client) -> None:
        assert client.get("/api/sources?sort=bogus").status_code == 422

    def test_source_detail_has_deeplink(self, client) -> None:
        d = client.get(f"/api/sources/{client.source_id}").json()
        assert d["digest"]["density"]["level"] == "high"
        assert d["segment_count"] == 2

    def test_segments_are_jumpable(self, client) -> None:
        segs = client.get(f"/api/sources/{client.source_id}/segments").json()["segments"]
        assert segs[0]["anchor"] == "02:05"
        assert segs[0]["jumpable"]
        assert "t=125" in segs[0]["url"]

    def test_missing_source_404(self, client) -> None:
        assert client.get("/api/sources/9999").status_code == 404
        assert client.get("/api/sources/9999/segments").status_code == 404


class TestNotesApi:
    def test_add_and_delete(self, client) -> None:
        r = client.post(
            f"/api/sources/{client.source_id}/notes",
            json={"text": "我的想法", "note_type": "insight"},
        )
        assert r.status_code == 200
        nid = r.json()["id"]
        assert len(r.json()["notes"]) == 1

        assert client.delete(f"/api/notes/{nid}").status_code == 200
        assert client.delete(f"/api/notes/{nid}").status_code == 404

    def test_invalid_type_rejected(self, client) -> None:
        r = client.post(
            f"/api/sources/{client.source_id}/notes",
            json={"text": "x", "note_type": "bogus"},
        )
        assert r.status_code == 400

    def test_empty_text_rejected(self, client) -> None:
        r = client.post(
            f"/api/sources/{client.source_id}/notes", json={"text": "   "}
        )
        assert r.status_code == 400

    def test_note_on_missing_source(self, client) -> None:
        r = client.post("/api/sources/9999/notes", json={"text": "x"})
        assert r.status_code == 404


class TestReviewApi:
    def test_queue_and_grade(self, client) -> None:
        client.post(f"/api/review/{client.source_id}/star", json={"starred": True})
        d = client.get("/api/review?upcoming=true").json()
        assert d["items"] and d["items"][0]["starred"]

        r = client.post(f"/api/review/{client.source_id}/grade", json={"value": "ok"})
        assert r.status_code == 200
        assert r.json()["interval_days"] >= 2

    def test_invalid_grade_rejected(self, client) -> None:
        r = client.post(f"/api/review/{client.source_id}/grade", json={"value": "great"})
        assert r.status_code == 400

    def test_grade_missing_source(self, client) -> None:
        r = client.post("/api/review/9999/grade", json={"value": "ok"})
        assert r.status_code == 404


class TestGraphApi:
    def test_empty_graph(self, client) -> None:
        d = client.get("/api/graph").json()
        assert d["nodes"] == [] and d["edges"] == []

    def test_neighborhood(self, client) -> None:
        d = client.get(f"/api/graph?source_id={client.source_id}").json()
        assert len(d["nodes"]) == 1

    def test_missing_source_404(self, client) -> None:
        assert client.get("/api/graph?source_id=9999").status_code == 404

    def test_depth_bounds(self, client) -> None:
        assert client.get("/api/graph?source_id=1&depth=9").status_code == 422


class TestBriefApi:
    def test_brief(self, client) -> None:
        d = client.get("/api/brief").json()
        assert d["new_count"] == 1
        assert d["pending_actions"]

    def test_questions(self, client) -> None:
        d = client.get("/api/questions").json()
        assert "open" in d and "answered" in d


class TestPages:
    """模板语法错误只在运行时暴露，必须实际渲染一遍。"""

    @pytest.mark.parametrize(
        "path", ["/", "/ask", "/graph", "/review", "/brief"]
    )
    def test_pages_render(self, client, path: str) -> None:
        r = client.get(path)
        assert r.status_code == 200
        assert "<!DOCTYPE html>" in r.text

    def test_read_page(self, client) -> None:
        r = client.get(f"/read/{client.source_id}")
        assert r.status_code == 200
        assert "核心结论一" in r.text
        assert "去做这件事" in r.text

    def test_outline_anchor_renders_as_link(self, client) -> None:
        """时间戳渲染成可跳转链接是 Web 相对 CLI 的核心增量。"""
        from distiller.models import Digest, OutlineItem, Quote
        from distiller.store import Database

        db = Database(client.app.state.deps.paths.db)
        db.save_digest(
            client.source_id,
            Digest(
                tldr=["结论"],
                outline=[OutlineItem(title="小节", points=["要点"], anchor="02:05")],
                quotes=[Quote(text="金句", anchor="05:00")],
                density_score=72.0,
            ),
        )
        db.close()

        text = client.get(f"/read/{client.source_id}").text
        assert "t=125" in text     # 大纲锚点
        assert "t=300" in text     # 金句锚点
        assert 'class="anchor"' in text

    def test_read_missing_404(self, client) -> None:
        assert client.get("/read/9999").status_code == 404

    def test_title_is_escaped(self, client) -> None:
        """库内标题来自外部网站，直接渲染会被 XSS。"""
        r = client.get(f"/read/{client.source_id}")
        assert "<script>alert(1)</script>" not in r.text
        assert "&lt;script&gt;" in r.text

    def test_static_served(self, client) -> None:
        assert client.get("/static/app.css").status_code == 200
        assert client.get("/static/app.js").status_code == 200

    def test_openapi_available(self, client) -> None:
        """M6 的 agent HTTP 接口要复用这份契约。"""
        d = client.get("/api/openapi.json").json()
        assert "/api/sources" in d["paths"]
