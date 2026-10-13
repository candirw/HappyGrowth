"""周期简报。

**不是罗列「这周看了什么」**——那种清单看一次就够，没有复用价值。
有价值的是这几类，它们都是「你自己想不起来但工具记得」的东西：

1. 新发现的关联（尤其结论冲突）—— 认知升级点
2. 悬着的可执行项 —— 最容易被遗忘
3. 遗留疑问是否已被新内容回答 —— 闭环感
4. 知识盲区 —— 反复提及但没深入的概念
5. 待重温 —— 该回头看的内容

刻意不调用 LLM：这些都能从库里直接算出来，
每周一次的例行任务不该产生 API 成本。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..store import Database
from .scheduler import ReviewScheduler


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ActionItem:
    source_id: int
    title: str
    text: str
    age_days: int


@dataclass
class Brief:
    period_days: int
    since: str
    new_count: int = 0
    total_chars: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    top_content: list[dict] = field(default_factory=list)
    low_value: list[dict] = field(default_factory=list)
    contradictions: list[dict] = field(default_factory=list)
    new_relations: list[dict] = field(default_factory=list)
    pending_actions: list[ActionItem] = field(default_factory=list)
    answered_questions: list[dict] = field(default_factory=list)
    open_questions: list[dict] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    due_reviews: int = 0
    my_notes: list[dict] = field(default_factory=list)
    cost_usd: float = 0.0
    hot_entities: list[dict] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.new_count
            or self.contradictions
            or self.pending_actions
            or self.answered_questions
            or self.due_reviews
            or self.gaps
        )


def build_brief(
    db: Database,
    *,
    days: int = 7,
    max_items: int = 8,
    scheduler: ReviewScheduler | None = None,
) -> Brief:
    """生成简报。纯数据库查询，零 LLM 成本。"""
    since_dt = utcnow() - timedelta(days=days)
    since = since_dt.isoformat()
    brief = Brief(period_days=days, since=since_dt.date().isoformat())

    # ---- 新增内容 ----
    rows = db.sources_since(since)
    brief.new_count = len(rows)
    scored: list[dict] = []
    for r in rows:
        brief.total_chars += int(r["char_count"] or 0)
        ctype = r["content_type"]
        brief.by_type[ctype] = brief.by_type.get(ctype, 0) + 1
        scored.append(
            {
                "id": int(r["id"]),
                "title": r["title"],
                "type": ctype,
                "density": r["density_score"],
            }
        )

    ranked = sorted(
        [s for s in scored if s["density"] is not None],
        key=lambda s: -s["density"],
    )
    brief.top_content = ranked[:max_items]
    # 低密度的也值得提示——说明这周在看水文，是个信号
    brief.low_value = [s for s in ranked if (s["density"] or 0) < 30][-3:]

    # ---- 新关联（冲突单列，价值最高）----
    for r in db.relations_since(since, limit=60):
        item = {
            "from_id": int(r["from_source"]),
            "to_id": int(r["to_source"]),
            "from_title": r["from_title"],
            "to_title": r["to_title"],
            "rel_type": r["rel_type"],
            "confidence": r["confidence"],
        }
        try:
            evidence = json.loads(r["evidence_json"] or "{}")
        except json.JSONDecodeError:
            evidence = {}
        item["new_claim"] = evidence.get("new_claim")
        item["old_claim"] = evidence.get("old_claim")
        item["note"] = evidence.get("note")

        if r["rel_type"] == "contradicts":
            brief.contradictions.append(item)
        else:
            brief.new_relations.append(item)
    brief.contradictions = brief.contradictions[:max_items]
    brief.new_relations = brief.new_relations[:max_items]

    # ---- 悬着的可执行项 ----
    brief.pending_actions = _collect_actions(db, limit=max_items)

    # ---- 疑问闭环 ----
    answered = db.conn.execute(
        "SELECT q.id, q.text, q.answered_by_source, s.title AS answer_title, "
        "       q2.title AS from_title "
        "FROM questions q "
        "LEFT JOIN sources s ON s.id = q.answered_by_source "
        "LEFT JOIN sources q2 ON q2.id = q.source_id "
        "WHERE q.status = 'answered' ORDER BY q.id DESC LIMIT ?",
        (max_items,),
    ).fetchall()
    brief.answered_questions = [
        {
            "text": r["text"],
            "answer_id": r["answered_by_source"],
            "answer_title": r["answer_title"],
            "from_title": r["from_title"],
        }
        for r in answered
    ]
    brief.open_questions = [
        {"id": int(r["id"]), "text": r["text"], "from_title": r["source_title"]}
        for r in db.open_questions(limit=max_items)
    ]

    # ---- 知识盲区 ----
    try:
        from ..knowledge import find_gaps

        brief.gaps = [
            {"name": g.name, "count": g.mention_count, "detail": g.detail}
            for g in find_gaps(db, limit=max_items)
        ]
    except Exception:  # noqa: BLE001 - 盲区分析失败不该让简报整体失败
        brief.gaps = []

    # ---- 待重温 ----
    sched = scheduler or ReviewScheduler(db)
    brief.due_reviews = sched.stats()["due"]

    # ---- 我的批注（自己的思考最值得回顾）----
    brief.my_notes = [
        {
            "source_id": int(r["source_id"]),
            "title": r["title"],
            "text": r["text"],
            "note_type": r["note_type"],
        }
        for r in db.recent_notes(since=since, limit=max_items)
    ]

    # ---- 高频概念 ----
    brief.hot_entities = [
        {"name": r["name"], "count": int(r["mention_count"])}
        for r in db.list_entities(min_mentions=2, limit=6)
    ]

    # ---- 成本 ----
    brief.cost_usd = db.month_cost()

    return brief


def _collect_actions(db: Database, *, limit: int = 8) -> list[ActionItem]:
    """收集未处理的可执行项。

    已被批注标记为 done 的会跳过——否则简报会反复提醒已完成的事。
    """
    done_texts: set[str] = set()
    for row in db.recent_notes(note_type="done", limit=200):
        done_texts.add(_action_key(row["text"]))

    items: list[ActionItem] = []
    now = utcnow()
    # 去重：不同内容常产出相同待办（如「补上 BM25 分支」），
    # 重复列出会让简报显得啰嗦，也稀释真正待处理的事项
    seen: set[str] = set()

    for row in db.pending_actions(limit=60):
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            continue
        actions = payload.get("actions") or []
        if not actions:
            continue
        try:
            created = datetime.fromisoformat(
                (row["created_at"] or "").replace("Z", "+00:00")
            )
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = max(0, (now - created).days)
        except ValueError:
            age = 0

        for action in actions:
            text = str(action).strip()
            if not text:
                continue
            key = _action_key(text)
            if key in done_texts or key in seen:
                continue
            seen.add(key)
            items.append(
                ActionItem(
                    source_id=int(row["source_id"]),
                    title=row["title"],
                    text=text,
                    age_days=age,
                )
            )

    # 悬置越久越该提醒
    items.sort(key=lambda a: -a.age_days)
    return items[:limit]


def _action_key(text: str) -> str:
    """待办去重键。忽略标点与空白差异，避免措辞微调导致重复。"""
    import re

    return re.sub(r"[\s，。、；；:：!！?？\.,;]+", "", text.lower())[:40]


def render_brief_markdown(brief: Brief) -> str:
    """渲染为 Markdown，便于导出或发到其他地方。"""
    lines = [
        f"# 知识简报 · 近 {brief.period_days} 天",
        "",
        f"*统计区间自 {brief.since}*",
        "",
    ]

    lines.append(
        f"本期新增 **{brief.new_count}** 篇 · "
        f"约 {brief.total_chars // 1000}k 字 · "
        f"待重温 **{brief.due_reviews}** 篇"
    )
    lines.append("")

    if brief.contradictions:
        lines += ["## ⚠️ 需要你判断的结论冲突", ""]
        for c in brief.contradictions:
            lines.append(f"- **#{c['from_id']}《{c['from_title']}》** ↔ "
                         f"**#{c['to_id']}《{c['to_title']}》**")
            if c.get("new_claim"):
                lines.append(f"  - 前者：{c['new_claim']}")
                lines.append(f"  - 后者：{c['old_claim']}")
            elif c.get("note"):
                lines.append(f"  - {c['note']}")
        lines.append("")

    if brief.pending_actions:
        lines += ["## 📌 悬着的可执行项", ""]
        for a in brief.pending_actions:
            age = f"（{a.age_days} 天前）" if a.age_days else ""
            lines.append(f"- [ ] {a.text} — 来自 #{a.source_id}《{a.title}》{age}")
        lines.append("")

    if brief.answered_questions:
        lines += ["## 🎯 旧疑问已有答案", ""]
        for q in brief.answered_questions:
            lines.append(
                f"- {q['text']} → #{q['answer_id']}《{q['answer_title']}》"
            )
        lines.append("")

    if brief.top_content:
        lines += ["## 本期高密度内容", ""]
        for c in brief.top_content:
            score = f"{c['density']:.0f}" if c["density"] is not None else "-"
            lines.append(f"- #{c['id']} 《{c['title']}》 · 密度 {score}")
        lines.append("")

    if brief.gaps:
        lines += ["## 知识盲区", ""]
        for g in brief.gaps:
            lines.append(f"- **{g['name']}**（提及 {g['count']} 次）—— {g['detail']}")
        lines.append("")

    if brief.open_questions:
        lines += ["## 仍未解决的疑问", ""]
        for q in brief.open_questions:
            lines.append(f"- {q['text']}")
        lines.append("")

    if brief.my_notes:
        lines += ["## 我的批注", ""]
        for n in brief.my_notes:
            lines.append(f"- [{n['note_type']}] {n['text']} — #{n['source_id']}")
        lines.append("")

    if brief.low_value:
        lines += ["## 低密度内容提示", ""]
        lines.append("本期以下内容信息密度偏低，可考虑调整信息源：")
        for c in brief.low_value:
            lines.append(f"- #{c['id']} 《{c['title']}》 · 密度 {c['density']:.0f}")
        lines.append("")

    lines += ["---", "", f"*本月 LLM 花费 ${brief.cost_usd:.4f}*"]
    return "\n".join(lines)
