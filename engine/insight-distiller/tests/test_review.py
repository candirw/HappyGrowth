"""重温与沉淀测试（M4）。

重点覆盖：
① 入队筛选（不该入队的必须拦住，否则重温变负担）
② 间隔算法边界（上下限、forgot 重置）
③ 简报去重与内容组织
④ Obsidian 双链的文件名安全性（跨平台关键）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from distiller.models import ContentType, Digest, RawContent, Segment, SourceStatus
from distiller.review import (
    GRADE_EASY,
    GRADE_FORGOT,
    GRADE_OK,
    ReviewScheduler,
    VaultExporter,
    build_brief,
    entity_page_name,
    next_schedule,
    render_brief_markdown,
    wikilink_title,
)
from distiller.review.brief import _action_key
from distiller.store import Database


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "r.db")
    yield database
    database.close()


def _seed(
    db: Database,
    title: str,
    *,
    density: float | None = 70.0,
    actions: list[str] | None = None,
    questions: list[str] | None = None,
    concepts: list[tuple[str, str]] | None = None,
) -> int:
    raw = RawContent(
        url=f"https://example.com/{title}",
        content_type=ContentType.ARTICLE,
        platform="test",
        title=title,
        segments=[Segment(idx=0, text=f"{title} 的正文内容，长度需要足够以便统计字数。")],
    )
    sid = db.upsert_source(raw, status=SourceStatus.DISTILLED)
    db.replace_segments(sid, raw.segments)

    from distiller.models import Concept

    digest = Digest(
        tldr=[f"{title} 的核心结论"],
        actions=actions or [],
        open_questions=questions or [],
        concepts=[Concept(term=t, definition=d) for t, d in (concepts or [])],
        density_score=density,
        density_verdict="测试",
    )
    db.save_digest(sid, digest)
    if density is not None:
        db.conn.execute(
            "UPDATE sources SET density_score = ? WHERE id = ?", (density, sid)
        )
        db.conn.commit()
    return sid


class TestNextSchedule:
    def test_interval_grows_on_ok(self) -> None:
        ease, interval = 2.5, 3
        prev = interval
        for _ in range(4):
            ease, interval = next_schedule(
                ease=ease, interval_days=interval, review_count=1, grade=GRADE_OK
            )
            assert interval > prev
            prev = interval

    def test_forgot_resets_and_lowers_ease(self) -> None:
        ease, interval = next_schedule(
            ease=2.5, interval_days=60, review_count=5, grade=GRADE_FORGOT
        )
        assert interval == 2
        assert ease < 2.5

    def test_easy_accelerates(self) -> None:
        _e_ok, i_ok = next_schedule(
            ease=2.5, interval_days=10, review_count=2, grade=GRADE_OK
        )
        e_easy, i_easy = next_schedule(
            ease=2.5, interval_days=10, review_count=2, grade=GRADE_EASY
        )
        assert i_easy > i_ok
        assert e_easy > 2.5

    def test_interval_capped(self) -> None:
        """不能算出「3 年后重温」这种无意义结果。"""
        _ease, interval = next_schedule(
            ease=3.0, interval_days=350, review_count=10, grade=GRADE_EASY
        )
        assert interval <= 365

    def test_ease_has_floor(self) -> None:
        ease = 2.5
        for _ in range(15):
            ease, _i = next_schedule(
                ease=ease, interval_days=5, review_count=3, grade=GRADE_FORGOT
            )
        assert ease >= 1.3

    def test_invalid_grade_rejected(self, db: Database) -> None:
        sid = _seed(db, "内容")
        sched = ReviewScheduler(db)
        with pytest.raises(ValueError, match="无效"):
            sched.grade(sid, "excellent")


class TestEnqueueFilter:
    """入队筛选是 M4 的核心判断——全部入队会让重温变成负担。"""

    def test_low_density_rejected(self, db: Database) -> None:
        sid = _seed(db, "水文", density=20.0, actions=["做点什么"])
        ok, reasons = ReviewScheduler(db, min_density=45.0).should_enqueue(sid)
        assert not ok
        assert any("密度" in r for r in reasons)

    def test_no_value_rejected(self, db: Database) -> None:
        """无可执行项/疑问/关联的内容，重温价值有限。"""
        sid = _seed(db, "纯资讯", density=55.0)
        ok, reasons = ReviewScheduler(db, min_density=45.0).should_enqueue(sid)
        assert not ok
        assert any("价值有限" in r for r in reasons)

    def test_actions_make_it_worth(self, db: Database) -> None:
        sid = _seed(db, "有待办", density=55.0, actions=["补上 BM25"])
        ok, reasons = ReviewScheduler(db).should_enqueue(sid)
        assert ok
        assert any("可执行项" in r for r in reasons)

    def test_open_questions_make_it_worth(self, db: Database) -> None:
        sid = _seed(db, "有疑问", density=50.0, questions=["如何分块？"])
        ok, _r = ReviewScheduler(db).should_enqueue(sid)
        assert ok

    def test_hub_by_relations(self, db: Database) -> None:
        """被多篇关联的内容是知识枢纽，值得重温。"""
        a = _seed(db, "枢纽", density=50.0)
        b = _seed(db, "乙", density=50.0)
        c = _seed(db, "丙", density=50.0)
        db.add_relation(a, b, "supports", confidence=0.9)
        db.add_relation(a, c, "extends", confidence=0.9)
        ok, reasons = ReviewScheduler(db).should_enqueue(a)
        assert ok
        assert any("枢纽" in r for r in reasons)

    def test_starred_always_enqueued(self, db: Database) -> None:
        """用户标记必看的，绕过所有价值判断。"""
        sid = _seed(db, "水文但我要看", density=10.0)
        sched = ReviewScheduler(db, min_density=45.0)
        sched.set_starred(sid, True)
        ok, reasons = sched.should_enqueue(sid)
        assert ok
        assert any("标记" in r for r in reasons)

    def test_require_value_can_be_disabled(self, db: Database) -> None:
        sid = _seed(db, "纯资讯", density=55.0)
        ok, _r = ReviewScheduler(db, require_value=False).should_enqueue(sid)
        assert ok

    def test_auto_enqueue_all_counts(self, db: Database) -> None:
        _seed(db, "好内容", density=70.0, actions=["做事"])
        _seed(db, "水文", density=15.0)
        added, skipped = ReviewScheduler(db, min_density=45.0).auto_enqueue_all()
        assert added == 1
        assert skipped == 1


class TestReviewQueue:
    def test_due_items_respects_due_date(self, db: Database) -> None:
        sid = _seed(db, "内容", actions=["做事"])
        sched = ReviewScheduler(db)
        sched.enqueue(sid)
        # 刚入队的默认 3 天后到期
        assert not sched.due_items()
        assert sched.due_items(include_future=True)

    def test_overdue_computed(self, db: Database) -> None:
        sid = _seed(db, "逾期", actions=["做事"])
        sched = ReviewScheduler(db)
        sched.enqueue(sid)
        past = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        db.conn.execute("UPDATE reviews SET due_at = ? WHERE source_id = ?", (past, sid))
        db.conn.commit()
        items = sched.due_items()
        assert items and items[0].overdue_days >= 4

    def test_starred_sorted_first(self, db: Database) -> None:
        a = _seed(db, "普通", actions=["x"])
        b = _seed(db, "必看", actions=["y"])
        sched = ReviewScheduler(db)
        sched.enqueue(a)
        sched.set_starred(b, True)
        items = sched.due_items(include_future=True)
        assert items[0].source_id == b

    def test_item_carries_actions_and_notes(self, db: Database) -> None:
        """重温要展示待办与批注——这才是重温的目的。"""
        sid = _seed(db, "内容", actions=["补 BM25", "测延迟"], questions=["如何分块？"])
        db.add_note(sid, "这个数字要验证", note_type="todo")
        sched = ReviewScheduler(db)
        sched.enqueue(sid)
        item = sched.due_items(include_future=True)[0]
        assert len(item.actions) == 2
        assert item.notes == ["这个数字要验证"]
        assert item.open_questions

    def test_display_reason_not_contradictory(self, db: Database) -> None:
        """回归测试：已在队列的项曾显示「密度偏低，不值得重温」——
        与它还在队列里自相矛盾。"""
        sid = _seed(db, "低密度但已入队", density=30.0, actions=["做事"])
        ReviewScheduler(db, min_density=20.0).enqueue(sid)
        # 事后调高阈值
        items = ReviewScheduler(db, min_density=60.0).due_items(include_future=True)
        assert items
        assert not any("不值得重温" in r for r in items[0].reasons)

    def test_grade_updates_schedule(self, db: Database) -> None:
        sid = _seed(db, "内容", actions=["做事"])
        sched = ReviewScheduler(db)
        sched.enqueue(sid)
        interval, due = sched.grade(sid, GRADE_OK)
        assert interval > 3
        row = db.conn.execute(
            "SELECT review_count, last_result FROM reviews WHERE source_id = ?", (sid,)
        ).fetchone()
        assert row["review_count"] == 1
        assert row["last_result"] == GRADE_OK

    def test_remove_from_queue(self, db: Database) -> None:
        sid = _seed(db, "内容", actions=["做事"])
        sched = ReviewScheduler(db)
        sched.enqueue(sid)
        sched.remove(sid)
        assert sched.stats()["total"] == 0


class TestNotes:
    def test_add_and_filter_by_type(self, db: Database) -> None:
        sid = _seed(db, "内容")
        db.add_note(sid, "想法一", note_type="comment")
        db.add_note(sid, "要做的事", note_type="todo")
        assert len(db.get_notes(sid)) == 2
        todos = db.get_notes(sid, note_type="todo")
        assert len(todos) == 1 and todos[0]["text"] == "要做的事"

    def test_delete(self, db: Database) -> None:
        sid = _seed(db, "内容")
        nid = db.add_note(sid, "临时")
        assert db.delete_note(nid)
        assert not db.delete_note(nid)


class TestBrief:
    def test_contradictions_separated(self, db: Database) -> None:
        """冲突要单列——认知升级多来自这里，不能混在普通关联里。"""
        a = _seed(db, "甲", actions=["x"])
        b = _seed(db, "乙", actions=["y"])
        db.add_relation(
            b, a, "contradicts", confidence=0.9,
            evidence={"new_claim": "必须 A", "old_claim": "不需要 A"},
        )
        db.add_relation(b, a, "same_topic", confidence=0.8)
        brief = build_brief(db, days=7)
        assert len(brief.contradictions) == 1
        assert brief.contradictions[0]["new_claim"] == "必须 A"
        assert all(r["rel_type"] != "contradicts" for r in brief.new_relations)

    def test_duplicate_actions_deduped(self, db: Database) -> None:
        """回归测试：不同内容产出相同待办时曾重复列出。"""
        _seed(db, "甲", actions=["补上 BM25 分支"])
        _seed(db, "乙", actions=["补上 BM25 分支", "另一件事"])
        brief = build_brief(db, days=7)
        texts = [a.text for a in brief.pending_actions]
        assert len(texts) == len(set(texts))
        assert len(texts) == 2

    def test_done_notes_hide_actions(self, db: Database) -> None:
        """标记完成的待办不该反复提醒。"""
        sid = _seed(db, "甲", actions=["补上 BM25 分支"])
        db.add_note(sid, "补上 BM25 分支", note_type="done")
        brief = build_brief(db, days=7)
        assert not brief.pending_actions

    def test_action_key_ignores_punctuation(self) -> None:
        assert _action_key("补上 BM25 分支。") == _action_key("补上BM25分支")

    def test_answered_questions_reported(self, db: Database) -> None:
        a = _seed(db, "提问方")
        b = _seed(db, "回答方")
        qid = db.add_question("如何分块？", source_id=a)
        db.answer_question(qid, b)
        brief = build_brief(db, days=7)
        assert brief.answered_questions
        assert brief.answered_questions[0]["answer_id"] == b

    def test_low_density_flagged(self, db: Database) -> None:
        """这周看了水文是个信号，值得提示。"""
        _seed(db, "水文", density=15.0)
        brief = build_brief(db, days=7)
        assert brief.low_value

    def test_empty_brief_detected(self, db: Database) -> None:
        assert build_brief(db, days=7).is_empty

    def test_markdown_render(self, db: Database) -> None:
        _seed(db, "内容", actions=["做事"])
        md = render_brief_markdown(build_brief(db, days=7))
        assert "# 知识简报" in md
        assert "- [ ] 做事" in md  # checkbox 形式便于在别处勾选

    def test_no_llm_needed(self, db: Database) -> None:
        """简报必须零 LLM 成本——每周例行任务不该产生 API 费用。"""
        _seed(db, "内容", actions=["做事"])
        before = db.month_cost()
        build_brief(db, days=7)
        assert db.month_cost() == before


class TestVaultExport:
    def test_wikilink_id_prefix_avoids_collision(self) -> None:
        """同名内容很常见（系列视频），带 id 前缀才能区分。"""
        a = wikilink_title("第一期", source_id=1)
        b = wikilink_title("第一期", source_id=2)
        assert a != b

    def test_wikilink_strips_unsafe_chars(self) -> None:
        """[ ] | # ^ 会破坏 Obsidian 链接语法。"""
        link = wikilink_title("标题[含]特殊|字符#锚^点", source_id=3)
        for ch in "[]|#^":
            assert ch not in link

    def test_entity_page_name_safe(self) -> None:
        assert "[" not in entity_page_name("概念[X]")

    def test_export_structure(self, db: Database, tmp_path) -> None:
        a = _seed(db, "甲文", actions=["做事"], concepts=[("RAG", "检索增强生成")])
        b = _seed(db, "乙文", concepts=[("RAG", "检索增强生成")])
        eid = db.upsert_entity("RAG", definition="检索增强生成")
        db.add_mention(eid, a)
        db.add_mention(eid, b)
        db.add_relation(a, b, "supports", confidence=0.9)

        out = tmp_path / "vault"
        stats = VaultExporter(db, min_entity_mentions=1).export(out)

        assert stats.notes == 2
        assert stats.entities >= 1
        assert (out / "知识库总览.md").exists()

        note = (out / "notes").glob("*甲文*.md")
        content = next(note).read_text(encoding="utf-8")
        # 关系与概念都必须是双链形式，否则 Obsidian 图谱连不起来
        assert "[[" in content
        assert "## 关联" in content

        concept = (out / "concepts" / "RAG.md").read_text(encoding="utf-8")
        assert "出现在 2 篇内容中" in concept

    def test_actions_are_checkboxes(self, db: Database, tmp_path) -> None:
        """Obsidian 里能直接勾选，否则待办只是死文本。"""
        _seed(db, "有待办", actions=["补上 BM25"])
        out = tmp_path / "vault"
        VaultExporter(db).export(out, include_entities=False, include_moc=False)
        content = next((out / "notes").glob("*.md")).read_text(encoding="utf-8")
        assert "- [ ] 补上 BM25" in content

    def test_contradiction_evidence_in_export(self, db: Database, tmp_path) -> None:
        a = _seed(db, "甲文")
        b = _seed(db, "乙文")
        db.add_relation(
            a, b, "contradicts", confidence=0.9,
            evidence={"new_claim": "必须 A", "old_claim": "不需要 A"},
        )
        out = tmp_path / "vault"
        VaultExporter(db).export(out, include_entities=False, include_moc=False)
        content = next((out / "notes").glob("*甲文*.md")).read_text(encoding="utf-8")
        assert "必须 A" in content and "不需要 A" in content

    def test_skips_undigested(self, db: Database, tmp_path) -> None:
        raw = RawContent(
            url="https://example.com/pending",
            content_type=ContentType.ARTICLE,
            platform="test",
            title="未浓缩",
            segments=[Segment(idx=0, text="内容")],
        )
        db.upsert_source(raw, status=SourceStatus.FETCHED)
        stats = VaultExporter(db).export(tmp_path / "v")
        assert stats.notes == 0
        assert stats.skipped


class TestQuestionDedupe:
    def test_same_question_shown_once(self, db: Database) -> None:
        """回归测试：不同内容留下相同疑问时曾重复展示。"""
        a = _seed(db, "甲")
        b = _seed(db, "乙")
        db.add_question("长文档如何分块？", source_id=a)
        db.add_question("长文档如何分块？", source_id=b)
        rows = db.open_questions()
        assert len(rows) == 1
