"""Agent 接口测试（M6）。

重点覆盖：
① **契约的 required 约束** —— 这是整个设计的核心防线，
   让 agent 不可能拿到「没出处、没时效、没权威度」的裸结论
② 时效过滤与权威度门槛
③ token 预算裁剪
④ coverage 诚实性（空库必须返回 empty）
⑤ 共识/分歧/存疑分组不合并观点
⑥ 安全默认值（写接口关闭、非本机需 token）
⑦ MCP 与 HTTP 契约同源
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from distiller.agent import (
    AgentService,
    Authority,
    ClaimType,
    KnowledgeBase,
    SearchRequest,
    Sufficiency,
)
from distiller.agent.schemas import Claim, SearchResponse, authority_rank
from distiller.config import AgentConfig, Config, DomainAgentConfig
from distiller.models import ContentType, Digest, RawContent, Segment, SourceStatus
from distiller.store import Database

TODAY = date.today()


def _cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.storage.data_dir = str(tmp_path)
    cfg.agent = AgentConfig(
        domains={
            "finance": DomainAgentConfig(
                require_as_of=True,
                default_max_staleness_days=180,
                authority_weights={"official": 1.0, "self_media": 0.4},
                disclaimer=True,
                disclaimer_text="不构成投资建议。",
            )
        }
    )
    return cfg


def _seed(
    db: Database,
    title: str,
    *,
    authority: str = "self_media",
    days_ago: int = 10,
    domain: str | None = "finance",
    claims: list[dict] | None = None,
    density: float = 70.0,
) -> int:
    raw = RawContent(
        url=f"https://example.com/{title}",
        content_type=ContentType.ARTICLE,
        platform="web",
        title=title,
        segments=[Segment(idx=0, text=f"{title} 的正文内容。")],
    )
    sid = db.upsert_source(raw, status=SourceStatus.DISTILLED)
    db.replace_segments(sid, raw.segments)
    db.save_digest(sid, Digest(tldr=["结论"], density_score=density))
    db.conn.execute(
        "UPDATE sources SET density_score = ?, authority = ?, domains_json = ?, "
        "publish_at = ? WHERE id = ?",
        (
            density,
            authority,
            json.dumps([domain] if domain else []),
            (TODAY - timedelta(days=days_ago)).isoformat(),
            sid,
        ),
    )
    db.conn.commit()

    rows = []
    for c in claims or []:
        rows.append(
            {
                "subject": c.get("subject", "主题"),
                "predicate": c["predicate"],
                "claim_type": c.get("type", "fact"),
                "confidence": c.get("conf", 0.9),
                "as_of": (
                    None
                    if c.get("no_date")
                    else (TODAY - timedelta(days=c.get("age", days_ago))).isoformat()
                ),
                "numeric": c.get("numeric", {}),
                "authority": authority,
                "domain": domain,
                "segment_idxs": [0],
            }
        )
    if rows:
        db.replace_claims(sid, rows)
    return sid


@pytest.fixture
def kb(tmp_path):
    cfg = _cfg(tmp_path)
    db = Database(tmp_path / "distiller.db")
    _seed(
        db, "官方公告", authority="official", days_ago=20,
        claims=[
            {"subject": "LPR", "predicate": "1年期LPR为3.0%", "type": "fact",
             "numeric": {"值": 3.0, "单位": "%"}, "conf": 0.98},
        ],
    )
    _seed(
        db, "自媒体观点", authority="self_media", days_ago=300,
        claims=[
            {"subject": "长期国债", "predicate": "长债配置价值凸显", "type": "opinion",
             "conf": 0.7},
            {"subject": "长期国债", "predicate": "明年利率将下行", "type": "prediction",
             "conf": 0.5},
        ],
    )
    _seed(
        db, "媒体数据", authority="media", days_ago=15,
        claims=[
            {"subject": "收益率", "predicate": "10年期国债收益率2.1%", "type": "data",
             "numeric": {"值": 2.1}, "conf": 0.92},
        ],
    )
    _seed(
        db, "无日期内容", authority="media", days_ago=5,
        claims=[{"subject": "无期", "predicate": "缺少时间标注的主张", "no_date": True}],
    )
    _seed(db, "技术内容", authority="media", domain="tech",
          claims=[{"subject": "RAG", "predicate": "混合检索提升召回", "type": "method"}])
    db.close()

    with KnowledgeBase(config=cfg) as k:
        yield k


class TestContractRequired:
    """整个 M6 设计的核心防线。"""

    def test_claim_requires_provenance_fields(self) -> None:
        """staleness_days / authority / citation 必须是 required——
        从 schema 层面杜绝裸结论，比文档里写注意事项有效得多。"""
        required = set(Claim.model_json_schema()["required"])
        for field in ("staleness_days", "authority", "citation", "claim_type"):
            assert field in required, f"{field} 必须是 required"

    def test_cannot_build_claim_without_citation(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Claim(
                claim_id=1, predicate="x", claim_type=ClaimType.FACT,
                staleness_days=1, authority=Authority.MEDIA,
            )  # 缺 citation

    def test_search_response_requires_coverage(self) -> None:
        """没有 coverage，agent 无法判断知识边界。"""
        assert "coverage" in SearchResponse.model_json_schema()["required"]

    def test_authority_rank_ordering(self) -> None:
        assert authority_rank(Authority.OFFICIAL) > authority_rank(Authority.MEDIA)
        assert authority_rank(Authority.MEDIA) > authority_rank(Authority.SELF_MEDIA)
        assert authority_rank(Authority.SELF_MEDIA) > authority_rank(Authority.ANONYMOUS)
        assert authority_rank(None) == 0
        assert authority_rank("bogus") == 0


class TestSearch:
    def test_all_claims_carry_citation(self, kb) -> None:
        res = kb.search("", domain="finance", limit=50)
        assert res.claims
        for c in res.claims:
            assert c.citation.source_id > 0
            assert c.citation.title
            assert c.staleness_days >= 0

    def test_claim_type_filter(self, kb) -> None:
        """事实与观点分两次取，是 agent 正确使用知识库的关键。"""
        facts = kb.search("", domain="finance", claim_types=["fact", "data"], limit=50)
        assert facts.claims
        assert all(
            c.claim_type in (ClaimType.FACT, ClaimType.DATA) for c in facts.claims
        )

        opinions = kb.search("", domain="finance", claim_types=["opinion"], limit=50)
        assert all(c.claim_type == ClaimType.OPINION for c in opinions.claims)

    def test_domain_isolation(self, kb) -> None:
        """理财 agent 不该被技术内容干扰。"""
        res = kb.search("", domain="finance", limit=50)
        assert all(c.domain == "finance" for c in res.claims)
        assert not any("混合检索" in c.predicate for c in res.claims)

    def test_staleness_filter(self, kb) -> None:
        fresh = kb.search("", domain="finance", max_staleness_days=30, limit=50)
        assert all(c.staleness_days <= 30 for c in fresh.claims)
        assert any("陈旧" in w for w in fresh.warnings)

    def test_domain_default_staleness_applied(self, kb) -> None:
        """未传 max_staleness_days 时应用 domain 默认值（finance=180）。"""
        res = kb.search("", domain="finance", limit=50)
        assert all(c.staleness_days <= 180 for c in res.claims)

    def test_min_authority_filter(self, kb) -> None:
        res = kb.search("", domain="finance", min_authority="media", limit=50)
        assert res.claims
        assert all(
            authority_rank(c.authority) >= authority_rank(Authority.MEDIA)
            for c in res.claims
        )

    def test_facts_rank_above_opinions(self, kb) -> None:
        """价值排序：事实优先。决定 token 预算裁剪时保留谁。"""
        res = kb.search("", domain="finance", max_staleness_days=9999, limit=50)
        types = [c.claim_type for c in res.claims]
        first_fact = next(
            (i for i, t in enumerate(types) if t in (ClaimType.FACT, ClaimType.DATA)),
            None,
        )
        first_pred = next(
            (i for i, t in enumerate(types) if t == ClaimType.PREDICTION), None
        )
        if first_fact is not None and first_pred is not None:
            assert first_fact < first_pred

    def test_missing_as_of_warned(self, kb) -> None:
        """finance 配了 require_as_of，无时间标注要提示。"""
        res = kb.search("", domain="finance", max_staleness_days=99999, limit=50)
        if any(c.as_of is None for c in res.claims):
            assert any("时间标注" in w for w in res.warnings)

    def test_non_fact_claims_warned(self, kb) -> None:
        """不加类型过滤时，必须提醒结果含观点。"""
        res = kb.search("", domain="finance", max_staleness_days=9999, limit=50)
        if any(c.claim_type != ClaimType.FACT for c in res.claims):
            assert any("非事实" in w for w in res.warnings)

    def test_multiword_query(self, kb) -> None:
        """回归测试：整串 LIKE 会让「国债 收益率」这类多词查询必然失配。"""
        res = kb.search("国债 收益率", domain="finance", limit=50)
        assert res.claims

    def test_disclaimer_attached(self, kb) -> None:
        assert kb.search("", domain="finance").disclaimer == "不构成投资建议。"

    def test_no_disclaimer_for_other_domain(self, kb) -> None:
        assert kb.search("", domain="tech").disclaimer is None


class TestTokenBudget:
    def test_budget_truncates(self, kb) -> None:
        big = kb.search("", domain="finance", token_budget=100000, limit=50)
        small = kb.search("", domain="finance", token_budget=80, limit=50)
        assert len(small.claims) < len(big.claims)
        assert small.coverage.truncated_by_budget
        assert any("预算" in w for w in small.warnings)

    def test_budget_keeps_highest_value(self, kb) -> None:
        """裁剪要保留价值最高的，而不是随机截断。"""
        tiny = kb.search(
            "", domain="finance", token_budget=70, limit=50, max_staleness_days=9999
        )
        if tiny.claims:
            assert tiny.claims[0].claim_type in (ClaimType.FACT, ClaimType.DATA)

    def test_no_budget_no_truncation(self, kb) -> None:
        res = kb.search("", domain="finance", limit=50)
        assert not res.coverage.truncated_by_budget


class TestCoverage:
    """coverage 必须诚实——宁可让 agent 说「我没有依据」。"""

    def test_empty_on_no_match(self, kb) -> None:
        res = kb.search("完全不存在的主题xyzzy", domain="finance")
        assert res.coverage.sufficiency == Sufficiency.EMPTY
        assert any("没有匹配" in w for w in res.warnings)

    def test_semantic_threshold_configured(self) -> None:
        """回归测试：语义检索总返回「最相似」结果，不设门槛会让
        库里没有的主题也返回无关 claim，coverage 被误判成 sufficient，
        agent 就基于无关证据硬答（AGENT_API.md §9 要防的失败模式）。"""
        from distiller.agent.service import _SEMANTIC_MIN_SIM

        cfg = Config()
        assert cfg.agent.semantic_min_sim > 0
        # 实测定标：相关 0.53~0.68，无关 0.32~0.44
        assert 0.45 <= _SEMANTIC_MIN_SIM <= 0.65

    def test_semantic_skipped_without_index(self, tmp_path) -> None:
        """回归测试：没建索引时不该加载 embedding 模型——
        白等十几秒且必然失败（曾让测试从 2 秒涨到 60 秒）。"""
        import time

        cfg = _cfg(tmp_path)
        db = Database(tmp_path / "distiller.db")
        _seed(db, "内容", claims=[{"subject": "X", "predicate": "X 成立"}])
        db.close()

        with KnowledgeBase(config=cfg) as k:
            start = time.perf_counter()
            k.search("任意查询", domain="finance")
            # 未建索引时应立即返回，不触碰模型
            assert time.perf_counter() - start < 2.0

    def test_empty_domain(self, kb) -> None:
        res = kb.search("", domain="不存在的领域")
        assert res.coverage.sufficiency == Sufficiency.EMPTY

    def test_stats_reports_boundary(self, kb) -> None:
        s = kb.stats("finance")
        assert s.claim_count > 0
        assert s.by_claim_type
        assert s.by_authority
        assert s.disclaimer

    def test_stats_empty_domain(self, kb) -> None:
        s = kb.stats("空领域")
        assert s.sufficiency == Sufficiency.EMPTY
        assert s.claim_count == 0


class TestGrouping:
    def test_predictions_are_unverified(self, kb) -> None:
        """预测不能混进共识。"""
        g = kb.get_claims("长期国债", domain="finance", max_staleness_days=9999)
        assert not any(c.claim_type == ClaimType.PREDICTION for c in g.consensus)

    def test_conflicts_go_to_disputed(self, tmp_path) -> None:
        cfg = _cfg(tmp_path)
        db = Database(tmp_path / "distiller.db")
        a = _seed(db, "甲", claims=[{"subject": "X", "predicate": "X 是好的", "type": "opinion"}])
        b = _seed(db, "乙", claims=[{"subject": "X", "predicate": "X 是坏的", "type": "opinion"}])
        ca = db.conn.execute("SELECT id FROM claims WHERE source_id=?", (a,)).fetchone()["id"]
        cb = db.conn.execute("SELECT id FROM claims WHERE source_id=?", (b,)).fetchone()["id"]
        db.conn.execute("UPDATE claims SET contradicts_json=? WHERE id=?", (json.dumps([cb]), ca))
        db.conn.execute("UPDATE claims SET contradicts_json=? WHERE id=?", (json.dumps([ca]), cb))
        db.conn.commit()
        db.close()

        with KnowledgeBase(config=cfg) as k:
            g = k.get_claims("X", domain="finance")
            assert g.disputed
            assert any("分歧" in w for w in g.warnings)
            # 关键：不做观点合并
            assert not g.consensus

            cf = k.conflicts("X", domain="finance")
            assert len(cf.pairs) == 1
            pair = cf.pairs[0]
            assert pair.claim_a.claim_id != pair.claim_b.claim_id
            assert pair.note

    def test_no_conflict_still_warns_about_coverage(self, kb) -> None:
        """没发现冲突 ≠ 结论可靠，也可能是覆盖不足。"""
        cf = kb.conflicts("LPR", domain="finance")
        if not cf.pairs:
            assert any("覆盖不足" in w for w in cf.warnings)


class TestSourceAndTimeline:
    def test_get_source_levels(self, kb) -> None:
        res = kb.search("", domain="finance", limit=1)
        sid = res.claims[0].citation.source_id
        full = kb.get_source(sid)
        tldr = kb.get_source(sid, level="tldr")
        assert full and tldr
        assert tldr.tldr == full.tldr
        assert not tldr.outline  # tldr 级别不含大纲

    def test_get_source_missing(self, kb) -> None:
        assert kb.get_source(99999) is None

    def test_timeline_ordered(self, kb) -> None:
        tl = kb.timeline("长期国债", domain="finance")
        dates = [p.date for p in tl.points if p.date]
        assert dates == sorted(dates)

    def test_timeline_thin_warned(self, kb) -> None:
        tl = kb.timeline("LPR", domain="finance")
        if len(tl.points) < 2:
            assert any("不足" in w for w in tl.warnings)


# ---------- HTTP API ----------

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def api(tmp_path):
    cfg = _cfg(tmp_path)
    db = Database(tmp_path / "distiller.db")
    _seed(db, "官方公告", authority="official", days_ago=20,
          claims=[{"subject": "LPR", "predicate": "1年期LPR为3.0%", "type": "fact"}])
    db.close()

    from distiller.agent.http_api import create_agent_app

    with TestClient(create_agent_app(cfg)) as c:
        c.cfg = cfg
        yield c


class TestHttpApi:
    def test_stats(self, api) -> None:
        r = api.get("/v1/stats?domain=finance")
        assert r.status_code == 200
        assert r.json()["claim_count"] == 1

    def test_search(self, api) -> None:
        r = api.get("/v1/claims/search?q=LPR&domain=finance")
        assert r.status_code == 200
        claims = r.json()["claims"]
        assert claims
        # HTTP 通道也必须带全溯源字段
        assert claims[0]["citation"]["title"]
        assert "staleness_days" in claims[0]

    def test_topic_endpoints(self, api) -> None:
        for path in (
            "/v1/topics/LPR/claims?domain=finance",
            "/v1/topics/LPR/conflicts?domain=finance",
            "/v1/topics/LPR/timeline?domain=finance",
        ):
            assert api.get(path).status_code == 200

    def test_source_404(self, api) -> None:
        assert api.get("/v1/sources/9999").status_code == 404

    def test_claim_404(self, api) -> None:
        assert api.get("/v1/claims/9999").status_code == 404

    def test_write_disabled_by_default(self, api) -> None:
        """写接口默认关闭是安全底线。"""
        r = api.post("/v1/ingest", json={"text": "内容"})
        assert r.status_code == 403

    def test_write_requires_payload(self, api) -> None:
        api.cfg.agent.allow_write = True
        assert api.post("/v1/ingest", json={}).status_code == 400
        api.cfg.agent.allow_write = False

    def test_token_enforced_when_configured(self, api) -> None:
        api.cfg.agent.read_token = "secret"
        assert api.get("/v1/stats").status_code == 401
        r = api.get("/v1/stats", headers={"Authorization": "Bearer secret"})
        assert r.status_code == 200
        api.cfg.agent.read_token = None

    def test_openapi_shares_schema(self, api) -> None:
        """MCP 与 HTTP 必须同源，否则契约会漂移。"""
        spec = api.get("/v1/openapi.json").json()
        claim = spec["components"]["schemas"]["Claim"]
        assert "staleness_days" in claim["required"]
        assert "citation" in claim["required"]

    def test_task_404(self, api) -> None:
        assert api.get("/v1/tasks/nonexistent").status_code == 404


# ---------- MCP ----------


class TestMcpServer:
    def test_tools_registered(self, tmp_path) -> None:
        pytest.importorskip("mcp")
        import asyncio

        from distiller.agent.mcp_server import build_server

        server, names = build_server(_cfg(tmp_path))
        tools = asyncio.run(server.list_tools())
        assert {t.name for t in tools} == set(names)
        assert "kb_search" in names
        assert "kb_stats" in names

    def test_write_tool_hidden_by_default(self, tmp_path) -> None:
        pytest.importorskip("mcp")
        from distiller.agent.mcp_server import build_server

        _server, names = build_server(_cfg(tmp_path))
        assert "kb_ingest" not in names

    def test_write_tool_exposed_when_allowed(self, tmp_path) -> None:
        pytest.importorskip("mcp")
        from distiller.agent.mcp_server import build_server

        cfg = _cfg(tmp_path)
        cfg.agent.allow_write = True
        _server, names = build_server(cfg)
        assert "kb_ingest" in names

    def test_all_read_tools_annotated(self, tmp_path) -> None:
        """规范要求客户端把 annotations 视为不可信，
        但服务端仍应正确声明。"""
        pytest.importorskip("mcp")
        import asyncio

        from distiller.agent.mcp_server import build_server

        server, _names = build_server(_cfg(tmp_path))
        tools = asyncio.run(server.list_tools())
        for t in tools:
            assert t.annotations is not None
            assert t.annotations.read_only_hint is True

    def test_output_schema_from_pydantic(self, tmp_path) -> None:
        """outputSchema 自动从 Pydantic 生成，这是防漂移的关键。"""
        pytest.importorskip("mcp")
        import asyncio

        from distiller.agent.mcp_server import build_server

        server, _names = build_server(_cfg(tmp_path))
        tools = asyncio.run(server.list_tools())
        search = next(t for t in tools if t.name == "kb_search")
        assert search.output_schema is not None
        claim = search.output_schema["$defs"]["Claim"]
        assert "staleness_days" in claim["required"]
        assert "citation" in claim["required"]


# ---------- 订阅 ----------


class TestSubscriptions:
    @pytest.fixture
    def db(self, tmp_path):
        database = Database(tmp_path / "s.db")
        yield database
        database.close()

    def test_crud(self, db: Database) -> None:
        sid = db.add_subscription("daily", "B站 某UP主 理财", domain="finance")
        assert db.get_subscription("daily")["id"] == sid
        assert db.get_subscription(sid)["name"] == "daily"
        assert len(db.list_subscriptions()) == 1

        db.set_subscription_enabled(sid, False)
        assert not db.list_subscriptions(enabled_only=True)

        assert db.delete_subscription(sid)
        assert not db.delete_subscription(sid)

    def test_new_subscription_is_due(self, db: Database) -> None:
        db.add_subscription("daily", "指令")
        assert len(db.due_subscriptions()) == 1

    def test_not_due_after_run(self, db: Database) -> None:
        sid = db.add_subscription("daily", "指令", interval_hours=24)
        run_id = db.start_subscription_run(sid)
        db.finish_subscription_run(run_id, sid, status="ok", added=2)
        assert not db.due_subscriptions()

    def test_due_again_after_interval(self, db: Database) -> None:
        sid = db.add_subscription("daily", "指令", interval_hours=1)
        run_id = db.start_subscription_run(sid)
        db.finish_subscription_run(run_id, sid, status="ok")
        old = (TODAY - timedelta(days=2)).isoformat() + "T00:00:00+00:00"
        db.conn.execute(
            "UPDATE subscriptions SET last_run_at = ? WHERE id = ?", (old, sid)
        )
        db.conn.commit()
        assert len(db.due_subscriptions()) == 1

    def test_seen_dedup_across_runs(self, db: Database) -> None:
        """跨运行去重必须独立于 sources 表——被密度门槛拒掉的
        内容不进 sources，否则每次都会重新抓取评估同一批水文。"""
        sid = db.add_subscription("daily", "指令")
        url = "https://example.com/a"
        assert not db.is_seen(sid, url)
        db.mark_seen(sid, url, "low_density")
        assert db.is_seen(sid, url)
        assert db.seen_count(sid) == 1

    def test_stats_accumulate(self, db: Database) -> None:
        sid = db.add_subscription("daily", "指令")
        for _ in range(3):
            run_id = db.start_subscription_run(sid)
            db.finish_subscription_run(
                run_id, sid, status="ok", added=2, skipped=1, cost_usd=0.01
            )
        row = db.get_subscription(sid)
        assert row["total_runs"] == 3
        assert row["total_added"] == 6
        assert abs(row["total_cost_usd"] - 0.03) < 1e-6

    def test_failure_recorded(self, db: Database) -> None:
        sid = db.add_subscription("daily", "指令")
        run_id = db.start_subscription_run(sid)
        db.finish_subscription_run(run_id, sid, status="failed", note="网络错误")
        row = db.get_subscription(sid)
        assert row["last_status"] == "failed"
        assert row["last_error"] == "网络错误"

    def test_digest_markdown(self) -> None:
        from distiller.subscribe import RunResult, render_digest_markdown

        md = render_digest_markdown(
            [
                RunResult(
                    sub_id=1, name="finance", added=1, candidates=5, skipped=4,
                    added_items=[
                        {"source_id": 3, "title": "标题", "url": "https://x.com",
                         "density": 72.0}
                    ],
                    skipped_reasons={"密度不足": 4},
                )
            ],
            cost_month=0.5,
        )
        assert "# 订阅日报" in md
        assert "密度不足 4 条" in md

    def test_digest_empty(self) -> None:
        from distiller.subscribe import render_digest_markdown

        assert "没有到期" in render_digest_markdown([])


class TestSchemaMigration:
    def test_v3_creates_subscription_tables(self, tmp_path) -> None:
        from distiller.store.db import SCHEMA_VERSION

        db = Database(tmp_path / "new.db")
        tables = {
            r["name"]
            for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"subscriptions", "subscription_runs", "subscription_seen"} <= tables
        # 断言当前版本而非写死数字——写死会让每次加迁移都误报
        assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
        db.close()

    def test_migration_is_idempotent(self, tmp_path) -> None:
        """迁移脚本全用 IF NOT EXISTS，重复执行必须安全。"""
        from distiller.store.db import SCHEMA_VERSION

        path = tmp_path / "again.db"
        for _ in range(3):
            db = Database(path)
            db.close()
        db = Database(path)
        assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
        db.close()
