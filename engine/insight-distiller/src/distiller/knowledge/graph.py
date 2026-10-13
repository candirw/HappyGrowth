"""图谱分析与知识盲区。

`gaps` 是高价值功能：统计库中被反复提及但你从未深入的概念，
直接告出知识缺口——这是「不知道自己不知道」的破解方式。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..store import Database
from .linker import REL_LABELS


@dataclass
class GraphNode:
    id: int
    title: str
    content_type: str
    distance: int
    density: float | None = None


@dataclass
class GraphView:
    center_id: int
    center_title: str
    nodes: list[GraphNode] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.nodes)


def build_graph(db: Database, source_id: int, *, depth: int = 2, limit: int = 30) -> GraphView:
    src = db.get_source(source_id)
    if src is None:
        raise ValueError(f"找不到 #{source_id}")

    view = GraphView(center_id=source_id, center_title=src.title)
    for row in db.graph_neighbors(source_id, depth=depth, limit=limit):
        view.nodes.append(
            GraphNode(
                id=int(row["id"]),
                title=row["title"],
                content_type=row["content_type"],
                distance=int(row["dist"]),
                density=row["density_score"],
            )
        )
    for row in db.get_relations(source_id, limit=limit):
        icon, label = REL_LABELS.get(row["rel_type"], ("·", row["rel_type"]))
        view.relations.append(
            {
                "id": int(row["other_id"]),
                "title": row["other_title"],
                "rel_type": row["rel_type"],
                "label": label,
                "icon": icon,
                "confidence": row["confidence"],
                "direction": row["direction"],
            }
        )
    return view


@dataclass
class Gap:
    name: str
    mention_count: int
    kind: str            # undefined_concept | unread_reference
    detail: str = ""


def find_gaps(db: Database, *, limit: int = 20) -> list[Gap]:
    """知识盲区分析。

    两类缺口：
    ① 被反复提及但库中没有明确定义的概念
    ② 论文的高被引参考文献但你从未消化
    """
    gaps: list[Gap] = []

    for row in db.concept_gaps(limit=limit):
        gaps.append(
            Gap(
                name=row["name"],
                mention_count=int(row["mention_count"]),
                kind="undefined_concept",
                detail=f"在 {row['mention_count']} 篇内容中被提及，但库中没有明确定义",
            )
        )

    # 高频实体但只出现在少数内容里 → 可能是理解薄弱点
    for row in db.list_entities(min_mentions=3, limit=limit):
        sources = db.entity_sources(int(row["id"]))
        if len(sources) >= 3:
            continue
        gaps.append(
            Gap(
                name=row["name"],
                mention_count=int(row["mention_count"]),
                kind="thin_coverage",
                detail=f"提及 {row['mention_count']} 次但仅覆盖 {len(sources)} 篇内容",
            )
        )

    # 去重后按提及次数排序
    seen: set[str] = set()
    unique: list[Gap] = []
    for gap in sorted(gaps, key=lambda g: -g.mention_count):
        if gap.name in seen:
            continue
        seen.add(gap.name)
        unique.append(gap)
    return unique[:limit]


@dataclass
class TimelineEntry:
    source_id: int
    title: str
    date: str | None
    claim_type: str | None = None
    predicate: str | None = None
    as_of: str | None = None


def build_timeline(
    db: Database, topic: str, *, limit: int = 20
) -> list[TimelineEntry]:
    """某主题的认知演进时间线。

    优先用 claim 的 as_of（主张对应时间），
    退化为内容发布时间——两者语义不同，前者更准确。
    """
    entries: list[TimelineEntry] = []

    for row in db.search_claims(subject=topic, limit=limit * 2):
        entries.append(
            TimelineEntry(
                source_id=int(row["source_id"]),
                title=row["title"],
                date=row["as_of"] or (row["publish_at"] or "")[:10] or None,
                claim_type=row["claim_type"],
                predicate=row["predicate"],
                as_of=row["as_of"],
            )
        )

    if not entries:
        # 没有 claim 时退化为按内容检索
        rows = db.search_segments(topic, limit=limit)
        seen: set[int] = set()
        for row in rows:
            sid = int(row["source_id"])
            if sid in seen:
                continue
            seen.add(sid)
            src = db.get_source(sid)
            if src is None:
                continue
            entries.append(
                TimelineEntry(
                    source_id=sid,
                    title=src.title,
                    date=src.publish_at.date().isoformat() if src.publish_at else None,
                )
            )

    # 有日期的按时间排序，无日期的排末尾
    entries.sort(key=lambda e: (e.date is None, e.date or ""))
    return entries[:limit]
