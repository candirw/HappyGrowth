"""知识关联测试（M3.5）。

重点覆盖三类高风险逻辑：
① 实体归一与变体合并（错了会让图谱充满近似重复项）
② claim 的数字校验（LLM 编造数值最危险——agent 会拿去计算）
③ 关联漏斗的成本控制与冲突判定的证据要求
"""

from __future__ import annotations

import pytest

from distiller.knowledge.claims import (
    Claim,
    _normalize_as_of,
    _verify_numeric,
)
from distiller.knowledge.domain import DomainClassifier
from distiller.knowledge.entity import (
    EntityNormalizer,
    _is_variant,
    collect_entities,
    extract_rule_entities,
)
from distiller.knowledge.graph import build_graph, build_timeline, find_gaps
from distiller.knowledge.linker import (
    REL_CONTRADICTS,
    REL_SAME_TOPIC,
    Linker,
)
from distiller.models import ContentType, RawContent, Segment, SourceStatus
from distiller.store import Database


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "k.db")
    yield database
    database.close()


def _seed(db: Database, title: str, texts: list[str]) -> int:
    raw = RawContent(
        url=f"https://example.com/{title}",
        content_type=ContentType.ARTICLE,
        platform="test",
        title=title,
        segments=[Segment(idx=i, text=t) for i, t in enumerate(texts)],
    )
    sid = db.upsert_source(raw, status=SourceStatus.DISTILLED)
    db.replace_segments(sid, raw.segments)
    return sid


class TestEntityNormalizer:
    def test_chinese_english_mapped_to_same_canonical(self) -> None:
        """不归一就无法跨内容关联——中英文写法必须映射到同一实体。"""
        n = EntityNormalizer()
        assert n.normalize("检索增强生成") == "RAG"
        assert n.normalize("Retrieval-Augmented Generation") == "RAG"
        assert n.normalize("RAG") == "RAG"

    def test_plural_reduced(self) -> None:
        assert EntityNormalizer().normalize("embeddings") == "Embedding"

    def test_user_aliases_merged(self) -> None:
        n = EntityNormalizer({"可转债": ["转债", "convertible bond"]})
        assert n.normalize("转债") == "可转债"
        # 内置词典不能被用户词典覆盖掉
        assert n.normalize("大模型") == "LLM"

    def test_stopwords_rejected(self) -> None:
        n = EntityNormalizer()
        for junk in ("the", "方法", "这个", "Figure", "123", "a"):
            assert not n.is_valid(n.normalize(junk)), junk

    def test_sentence_fragment_rejected(self) -> None:
        """实体名不该是句子片段，否则实体表会被灌满垃圾。"""
        n = EntityNormalizer()
        assert not n.is_valid("这是一个非常长的中文句子片段应该被过滤")
        assert not n.is_valid("We propose a new method for retrieval")
        assert not n.is_valid("向量检索，需要补充")

    def test_valid_entities_accepted(self) -> None:
        n = EntityNormalizer()
        for good in ("RAG", "BERT-Large", "向量数据库", "Vector Database"):
            assert n.is_valid(good), good


class TestVariantMerge:
    def test_version_variants_detected(self) -> None:
        assert _is_variant("bert", "bert-large")
        assert _is_variant("gpt", "gpt-3")
        assert _is_variant("t5", "t5-base")
        assert _is_variant("llama", "llama-2")

    def test_different_entities_not_merged(self) -> None:
        """误合并比不合并更糟——会丢失真实的不同实体。"""
        assert not _is_variant("bert", "roberta")
        assert not _is_variant("rag", "ragtime")
        assert not _is_variant("gpt", "gptq")

    def test_collect_merges_variants(self) -> None:
        text = "We evaluate BERT, BERT-Large, GPT-3 and GPT on the task."
        names = {e.name for e in collect_entities(text)}
        # 保留更具体的版本，不同时出现基础名与版本名
        assert not ({"BERT", "BERT-Large"} <= names)
        assert not ({"GPT", "GPT-3"} <= names)

    def test_llm_definition_preserved_on_merge(self) -> None:
        """合并时不能丢定义。"""
        text = "BERT and BERT-Large are compared."
        payload = {"concepts": [{"term": "BERT", "definition": "双向编码器"}]}
        entities = {e.name: e for e in collect_entities(text, payload)}
        assert any(e.definition == "双向编码器" for e in entities.values())


class TestRuleExtraction:
    def test_acronyms_and_models(self) -> None:
        text = "We use RAG with BM25 and FAISS. GPT-4 and BERT were tested."
        names = {e.name for e in extract_rule_entities(text)}
        assert {"RAG", "BM25", "FAISS"} <= names

    def test_paper_noise_filtered(self) -> None:
        text = "See Figure 2 and Table 3 in Section 4."
        names = {e.name for e in extract_rule_entities(text)}
        n = EntityNormalizer()
        valid = {x for x in names if n.is_valid(x)}
        assert "Figure" not in valid
        assert "Table" not in valid


class TestDomainClassifier:
    def test_finance_detected(self) -> None:
        c = DomainClassifier()
        text = "债券基金的收益率与利率走势相关，资产配置需考虑通胀。"
        assert c.primary(text) == "finance"

    def test_tech_detected(self) -> None:
        c = DomainClassifier()
        text = "使用 RAG 架构时，向量检索配合 BM25 能提升召回。模型微调需要标注数据。"
        assert c.primary(text) == "tech"

    def test_single_keyword_not_enough(self) -> None:
        """单个关键词可能是偶然提及，不该判定归属。"""
        c = DomainClassifier()
        assert c.primary("今天聊聊摄影之外的话题") is None

    def test_finance_rule_has_strict_settings(self) -> None:
        """理财领域必须要求时间标注并降权自媒体——金融知识会过期。"""
        rule = DomainClassifier().rule("finance")
        assert rule.require_as_of is True
        assert rule.extract_numeric is True
        assert rule.disclaimer is True
        assert rule.authority_weights["self_media"] < rule.authority_weights["official"]

    def test_custom_domain_merges_keywords(self) -> None:
        c = DomainClassifier({"finance": {"keywords": ["可转债"]}})
        rule = c.rule("finance")
        assert "可转债" in rule.keywords
        assert "基金" in rule.keywords  # 内置词保留


class TestClaimVerification:
    def test_fabricated_number_rejected(self) -> None:
        """核心防线：LLM 编造的数值必须丢弃，否则 agent 会拿去计算。"""
        claim = Claim(predicate="提升了 99.9%", numeric={"提升": 99.9})
        ok, reason = _verify_numeric(claim, "实验显示提升了 3.2%")
        assert not ok
        assert "未出现在原文" in reason

    def test_real_number_accepted(self) -> None:
        claim = Claim(predicate="召回率 42%", numeric={"召回率": 42})
        ok, _ = _verify_numeric(claim, "向量检索召回率仅 42%，需补充 BM25")
        assert ok

    def test_trailing_zero_tolerated(self) -> None:
        """7.20 与 7.2 应视为同一数字，否则会误丢有效 claim。"""
        claim = Claim(predicate="年化 7.2%", numeric={"年化": 7.2})
        ok, _ = _verify_numeric(claim, "该基金年化收益率为 7.20%")
        assert ok

    def test_no_numeric_always_passes(self) -> None:
        ok, _ = _verify_numeric(Claim(predicate="这是观点"), "任意文本")
        assert ok


class TestAsOfNormalization:
    def test_formats(self) -> None:
        assert _normalize_as_of("2026-08-15") == "2026-08-15"
        assert _normalize_as_of("2026-08") == "2026-08"
        assert _normalize_as_of("2026") == "2026"
        assert _normalize_as_of("2026年8月") == "2026-08"
        assert _normalize_as_of("2026年") == "2026"

    def test_invalid_dropped_not_guessed(self) -> None:
        """时间标注宁缺勿错——猜错会让 agent 用过期知识。"""
        assert _normalize_as_of("最近") is None
        assert _normalize_as_of("unknown") is None
        assert _normalize_as_of(None) is None


class TestLinkerFunnel:
    def test_entity_cooccurrence_is_free(self, db: Database) -> None:
        """漏斗第一级必须零 LLM 调用。"""
        a = _seed(db, "甲", ["混合检索需要 BM25"])
        b = _seed(db, "乙", ["BM25 与向量检索结合"])
        for name in ("BM25", "混合检索"):
            eid = db.upsert_entity(name)
            db.add_mention(eid, a)
            db.add_mention(eid, b)

        linker = Linker(db, router=None, retriever=None)
        result = linker.link(a, digest=None)
        assert result.llm_calls == 0
        assert any("零成本" in n for n in result.notes)

    def test_no_llm_falls_back_to_same_topic(self, db: Database) -> None:
        """无 LLM 时只落同主题关系，不猜测具体类型。"""
        a = _seed(db, "甲", ["内容甲"])
        b = _seed(db, "乙", ["内容乙"])
        for name in ("RAG", "Embedding"):
            eid = db.upsert_entity(name)
            db.add_mention(eid, a)
            db.add_mention(eid, b)

        Linker(db, router=None, retriever=None).link(a, digest=None)
        rels = db.get_relations(a)
        assert rels
        assert all(r["rel_type"] == REL_SAME_TOPIC for r in rels)
        # 未经语义确认的关系必须标注出来
        assert any("未经语义确认" in (r["evidence_json"] or "") for r in rels)

    def test_contradiction_without_evidence_downgraded(self, db: Database) -> None:
        """冲突判定缺少双方证据时不可信，应降级为同主题。

        这是刻意设计：contradicts 是最高价值也最容易误判的关系，
        没有原文依据的冲突断言会误导用户。
        """
        import json

        from distiller.llm import register_provider
        from distiller.llm.base import LLMProvider, LLMResponse
        from distiller.config import Config, ProviderConfig, TaskConfig
        from distiller.llm import LLMRouter

        @register_provider("noevidence")
        class NoEvidence(LLMProvider):
            def complete(self, prompt, **kwargs):
                return LLMResponse(
                    text=json.dumps(
                        {"relations": [{"id": 1, "rel_type": "contradicts",
                                        "confidence": 0.9, "note": "冲突"}]}
                    ),
                    model="m", provider=self.name,
                )

            def health(self):
                return True, ""

        a = _seed(db, "甲", ["内容甲"])
        b = _seed(db, "乙", ["内容乙"])
        from distiller.models import Digest

        db.save_digest(a, Digest(tldr=["甲的结论"]))
        db.save_digest(b, Digest(tldr=["乙的结论"]))
        for name in ("RAG", "BM25"):
            eid = db.upsert_entity(name)
            db.add_mention(eid, a)
            db.add_mention(eid, b)

        cfg = Config(
            providers={"ne": ProviderConfig(type="noevidence", models={"chat": "m"})},
            tasks={"map": TaskConfig(provider="ne")},
        )
        linker = Linker(db, router=LLMRouter(cfg), retriever=None)
        result = linker.link(b, digest=db.get_latest_digest(b))
        types = {r["rel_type"] for r in result.relations}
        assert REL_CONTRADICTS not in types

    def test_low_confidence_filtered(self, db: Database) -> None:
        """低置信度关系不展示，否则提示会变成骚扰。"""
        a = _seed(db, "甲", ["x"])
        b = _seed(db, "乙", ["y"])
        db.add_relation(a, b, REL_SAME_TOPIC, confidence=0.3)
        assert not db.get_relations(a, min_confidence=0.7)
        assert db.get_relations(a, min_confidence=0.2)

    def test_rejected_relation_hidden(self, db: Database) -> None:
        """用户否决的关系不再展示。"""
        a = _seed(db, "甲", ["x"])
        b = _seed(db, "乙", ["y"])
        db.add_relation(a, b, REL_CONTRADICTS, confidence=0.9)
        rel_id = db.conn.execute("SELECT id FROM relations").fetchone()["id"]
        db.verify_relation(int(rel_id), accepted=False)
        assert not db.get_relations(a)


class TestGraphTraversal:
    def test_multi_hop_distance(self, db: Database) -> None:
        ids = [_seed(db, f"内容{i}", [f"正文{i}"]) for i in range(4)]
        db.add_relation(ids[0], ids[1], "cites", confidence=0.9)
        db.add_relation(ids[1], ids[2], "extends", confidence=0.8)
        db.add_relation(ids[2], ids[3], "supports", confidence=0.8)

        d1 = db.graph_neighbors(ids[0], depth=1)
        assert {int(r["id"]) for r in d1} == {ids[1]}

        d2 = db.graph_neighbors(ids[0], depth=2)
        assert {int(r["id"]) for r in d2} == {ids[1], ids[2]}
        dist = {int(r["id"]): int(r["dist"]) for r in d2}
        assert dist[ids[2]] == 2

    def test_no_infinite_loop_on_cycle(self, db: Database) -> None:
        """环状关系不能导致递归查询无限循环。"""
        a = _seed(db, "甲", ["x"])
        b = _seed(db, "乙", ["y"])
        db.add_relation(a, b, "cites", confidence=0.9)
        db.add_relation(b, a, "supports", confidence=0.9)
        rows = db.graph_neighbors(a, depth=4)
        assert len(rows) == 1

    def test_build_graph_view(self, db: Database) -> None:
        a = _seed(db, "中心", ["x"])
        b = _seed(db, "邻居", ["y"])
        db.add_relation(a, b, REL_CONTRADICTS, confidence=0.9)
        view = build_graph(db, a)
        assert view.center_title == "中心"
        assert view.relations[0]["rel_type"] == REL_CONTRADICTS
        assert view.relations[0]["direction"] == "out"


class TestEntityMerge:
    def test_merge_moves_mentions(self, db: Database) -> None:
        a = _seed(db, "甲", ["x"])
        keep = db.upsert_entity("RAG")
        drop = db.upsert_entity("检索增强")
        db.add_mention(keep, a)
        db.add_mention(drop, a)

        db.merge_entities(keep, drop)
        assert db.find_entity("检索增强") is not None  # 作为别名可查到
        assert db.find_entity("检索增强")["name"] == "RAG"
        assert db.conn.execute(
            "SELECT COUNT(*) AS n FROM entities"
        ).fetchone()["n"] == 1

    def test_prune_removes_low_frequency(self, db: Database) -> None:
        a = _seed(db, "甲", ["x"])
        rare = db.upsert_entity("偶然提及")
        db.add_mention(rare, a)
        common = db.upsert_entity("常见概念")
        b = _seed(db, "乙", ["y"])
        db.add_mention(common, a)
        db.add_mention(common, b)

        removed = db.prune_entities(min_mentions=2)
        assert removed == 1
        assert db.find_entity("常见概念") is not None
        assert db.find_entity("偶然提及") is None


class TestClaimStorage:
    def test_search_by_type_and_domain(self, db: Database) -> None:
        sid = _seed(db, "理财内容", ["债券收益率"])
        db.replace_claims(sid, [
            {"predicate": "LPR 为 3.0%", "claim_type": "fact", "domain": "finance",
             "numeric": {"LPR": 3.0}, "as_of": "2026-08"},
            {"predicate": "我认为该加仓", "claim_type": "opinion", "domain": "finance"},
            {"predicate": "明年利率会下行", "claim_type": "prediction", "domain": "finance"},
        ])
        facts = db.search_claims(domain="finance", claim_types=["fact"])
        assert len(facts) == 1
        assert facts[0]["claim_type"] == "fact"

        # agent 取事实与数据时不该拿到观点与预测
        hard = db.search_claims(domain="finance", claim_types=["fact", "data"])
        assert all(r["claim_type"] in ("fact", "data") for r in hard)

    def test_replace_is_idempotent(self, db: Database) -> None:
        sid = _seed(db, "内容", ["文本"])
        rows = [{"predicate": "某个主张陈述", "claim_type": "fact"}]
        db.replace_claims(sid, rows)
        db.replace_claims(sid, rows)
        assert db.count_claims() == 1


class TestGapsAndTimeline:
    def test_undefined_concept_reported(self, db: Database) -> None:
        a = _seed(db, "甲", ["x"])
        b = _seed(db, "乙", ["y"])
        eid = db.upsert_entity("神秘概念")  # 无定义
        db.add_mention(eid, a)
        db.add_mention(eid, b)
        gaps = find_gaps(db)
        assert any(g.name == "神秘概念" for g in gaps)

    def test_defined_concept_not_a_gap(self, db: Database) -> None:
        a = _seed(db, "甲", ["x"])
        b = _seed(db, "乙", ["y"])
        eid = db.upsert_entity("已懂概念", definition="有明确定义")
        db.add_mention(eid, a)
        db.add_mention(eid, b)
        assert not any(g.name == "已懂概念" and g.kind == "undefined_concept"
                       for g in find_gaps(db))

    def test_timeline_prefers_claim_as_of(self, db: Database) -> None:
        """时间线应用主张对应时间（as_of），而非内容发布时间。"""
        sid = _seed(db, "利率分析", ["利率走势"])
        db.replace_claims(sid, [
            {"predicate": "2024 年利率处于高位", "claim_type": "fact",
             "subject": "利率", "as_of": "2024-06"},
            {"predicate": "2026 年利率已下行", "claim_type": "fact",
             "subject": "利率", "as_of": "2026-03"},
        ])
        entries = build_timeline(db, "利率")
        dates = [e.as_of for e in entries if e.as_of]
        assert dates == sorted(dates)


class TestQuestionTracking:
    def test_open_and_answer(self, db: Database) -> None:
        a = _seed(db, "提问方", ["x"])
        b = _seed(db, "回答方", ["y"])
        qid = db.add_question("长文档如何分块？", source_id=a)
        assert len(db.open_questions()) == 1
        db.answer_question(qid, b)
        assert not db.open_questions()
