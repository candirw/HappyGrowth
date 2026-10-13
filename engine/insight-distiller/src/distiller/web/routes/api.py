"""JSON API。

供前端 fetch 使用，同时因为挂了 FastAPI 的 OpenAPI，
**M6 的 agent HTTP 接口可以直接复用这一层**（届时只加鉴权与稳定契约）。

写操作（批注、重温打分、关系确认）都放这里，页面只读。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request

from ...review import GRADES, ReviewScheduler, build_brief
from ..views import (
    density_band,
    digest_view,
    hits_view,
    notes_view,
    relations_view,
    segments_view,
    source_brief,
)

router = APIRouter(tags=["api"])


def _deps(request: Request):
    return request.app.state.deps


@router.get("/stats")
def stats(request: Request) -> dict[str, Any]:
    deps = _deps(request)
    with deps.db() as db:
        sources = db.list_sources(limit=10_000)
        scored = [s.density_score for s in sources if s.density_score is not None]
        sched = ReviewScheduler(
            db,
            min_density=deps.config.review.min_density,
            require_value=deps.config.review.require_value,
        )
        return {
            "sources": len(sources),
            "avg_density": round(sum(scored) / len(scored), 1) if scored else None,
            "claims": db.count_claims(),
            "notes": db.count_notes(),
            "open_questions": len(db.open_questions(limit=500)),
            "review": sched.stats(),
            "month_cost": round(db.month_cost(), 4),
            "budget": deps.config.limits.monthly_budget_usd,
        }


@router.get("/sources")
def list_sources(
    request: Request,
    domain: str | None = Query(None),
    content_type: str | None = Query(None),
    sort: str = Query("recent", pattern="^(recent|density|title)$"),
    min_density: float | None = Query(None),
    q: str | None = Query(None),
    limit: int = Query(200, le=2000),
) -> dict[str, Any]:
    with _deps(request).db() as db:
        items = [source_brief(s) for s in db.list_sources(limit=limit)]

    if domain:
        items = [i for i in items if domain in i["domains"]]
    if content_type:
        items = [i for i in items if i["content_type"] == content_type]
    if min_density is not None:
        items = [
            i for i in items
            if i["density"]["score"] is not None and i["density"]["score"] >= min_density
        ]
    if q:
        needle = q.lower()
        items = [
            i for i in items
            if needle in (i["title"] or "").lower()
            or needle in (i["author"] or "").lower()
        ]

    if sort == "density":
        # 未评分的排最后，而不是当成 0 分混在水文里
        items.sort(key=lambda i: (i["density"]["score"] is None, -(i["density"]["score"] or 0)))
    elif sort == "title":
        items.sort(key=lambda i: i["title"] or "")

    domains: dict[str, int] = {}
    types: dict[str, int] = {}
    for i in items:
        for d in i["domains"]:
            domains[d] = domains.get(d, 0) + 1
        types[i["content_type"]] = types.get(i["content_type"], 0) + 1

    return {
        "items": items,
        "total": len(items),
        "facets": {"domains": domains, "content_types": types},
    }


@router.get("/sources/{source_id}")
def get_source(request: Request, source_id: int) -> dict[str, Any]:
    with _deps(request).db() as db:
        src = db.get_source(source_id)
        if src is None:
            raise HTTPException(404, f"找不到内容 #{source_id}")
        digest = db.get_latest_digest(source_id)
        return {
            "source": source_brief(src),
            "digest": digest_view(src, digest) if digest else None,
            "relations": relations_view(db, source_id),
            "notes": notes_view(db, source_id),
            "segment_count": len(db.get_segments(source_id)),
        }


@router.get("/sources/{source_id}/segments")
def get_segments(request: Request, source_id: int) -> dict[str, Any]:
    """原文片段。按需加载——长视频转写可能上万字，不该塞进首屏。"""
    with _deps(request).db() as db:
        src = db.get_source(source_id)
        if src is None:
            raise HTTPException(404, f"找不到内容 #{source_id}")
        return {"segments": segments_view(db, src)}


@router.post("/sources/{source_id}/notes")
def add_note(
    request: Request,
    source_id: int,
    text: str = Body(..., embed=True),
    note_type: str = Body("comment", embed=True),
) -> dict[str, Any]:
    valid = ("comment", "todo", "disagree", "insight", "done")
    if note_type not in valid:
        raise HTTPException(400, f"note_type 须为 {valid} 之一")
    if not text.strip():
        raise HTTPException(400, "批注内容不能为空")

    with _deps(request).db() as db:
        if db.get_source(source_id) is None:
            raise HTTPException(404, f"找不到内容 #{source_id}")
        nid = db.add_note(source_id, text.strip(), note_type=note_type)
        return {"id": nid, "notes": notes_view(db, source_id)}


@router.delete("/notes/{note_id}")
def delete_note(request: Request, note_id: int) -> dict[str, Any]:
    with _deps(request).db() as db:
        if not db.delete_note(note_id):
            raise HTTPException(404, f"找不到批注 #{note_id}")
        return {"deleted": note_id}


@router.post("/search")
def search(
    request: Request,
    query: str = Body(..., embed=True),
    limit: int = Body(10, embed=True),
) -> dict[str, Any]:
    """混合检索，不调 LLM（零成本）。"""
    deps = _deps(request)
    from ...search import Retriever

    with deps.db() as db:
        retriever = Retriever(db, deps.config.retrieval)
        try:
            hits = retriever.search(query, top_n=limit)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"检索失败：{exc}") from exc
        return {"hits": hits_view(hits)}


@router.post("/ask")
def ask(
    request: Request,
    question: str = Body(..., embed=True),
    source_id: int | None = Body(None, embed=True),
    top_n: int | None = Body(None, embed=True),
) -> dict[str, Any]:
    """问答。回答带引用，无依据会明确说明。"""
    deps = _deps(request)
    from ...search import ChatEngine

    if not question.strip():
        raise HTTPException(400, "问题不能为空")

    with deps.db() as db:
        engine = ChatEngine(deps.config, db, profile=deps.profile)
        try:
            answer = engine.ask(
                question.strip(),
                source_ids=[source_id] if source_id else None,
                top_n=top_n,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"问答失败：{exc}") from exc

        return {
            "text": answer.text,
            "citations": answer.citations,
            "hits": hits_view(answer.hits),
            "cost": round(getattr(answer, "cost_usd", 0.0) or 0.0, 5),
        }


@router.get("/graph")
def graph(
    request: Request,
    source_id: int | None = Query(None),
    depth: int = Query(1, ge=1, le=3),
    limit: int = Query(60, le=300),
) -> dict[str, Any]:
    """关系图。给定 source_id 则取其邻域，否则取全库高置信关系。"""
    with _deps(request).db() as db:
        nodes: dict[int, dict] = {}
        edges: list[dict] = []

        def add_node(sid: int) -> None:
            if sid in nodes:
                return
            src = db.get_source(sid)
            if src is None:
                return
            nodes[sid] = {
                "id": sid,
                "title": src.title,
                "content_type": src.content_type.value,
                "domains": list(src.domains or []),
                "density": density_band(src.density_score),
            }

        if source_id is not None:
            if db.get_source(source_id) is None:
                raise HTTPException(404, f"找不到内容 #{source_id}")
            add_node(source_id)
            frontier = [source_id]
            seen_edges: set[tuple[int, int, str]] = set()
            for _ in range(depth):
                nxt: list[int] = []
                for sid in frontier:
                    for row in db.get_relations(sid, limit=limit):
                        other = int(row["other_id"])
                        add_node(other)
                        key = tuple(sorted((sid, other))) + (row["rel_type"],)
                        if key in seen_edges:
                            continue
                        seen_edges.add(key)
                        edges.append(
                            {
                                "source": sid,
                                "target": other,
                                "rel_type": row["rel_type"],
                                "confidence": round(float(row["confidence"] or 0), 2),
                            }
                        )
                        nxt.append(other)
                frontier = nxt
        else:
            rows = db.conn.execute(
                "SELECT * FROM relations "
                "WHERE (verified_by_user IS NULL OR verified_by_user = 1) "
                "ORDER BY confidence DESC LIMIT ?",
                (limit,),
            ).fetchall()
            for row in rows:
                a, b = int(row["from_source"]), int(row["to_source"])
                add_node(a)
                add_node(b)
                if a in nodes and b in nodes:
                    edges.append(
                        {
                            "source": a,
                            "target": b,
                            "rel_type": row["rel_type"],
                            "confidence": round(float(row["confidence"] or 0), 2),
                        }
                    )

        return {"nodes": list(nodes.values()), "edges": edges}


@router.get("/review")
def review_queue(
    request: Request,
    limit: int | None = Query(None),
    upcoming: bool = Query(False),
) -> dict[str, Any]:
    deps = _deps(request)
    with deps.db() as db:
        sched = ReviewScheduler(
            db,
            min_density=deps.config.review.min_density,
            require_value=deps.config.review.require_value,
        )
        items = sched.due_items(
            limit=limit or deps.config.review.daily_limit, include_future=upcoming
        )
        return {
            "stats": sched.stats(),
            "items": [
                {
                    "source_id": i.source_id,
                    "title": i.title,
                    "content_type": i.content_type,
                    "density": density_band(i.density),
                    "due_at": i.due_at,
                    "overdue_days": i.overdue_days,
                    "interval_days": i.interval_days,
                    "review_count": i.review_count,
                    "starred": i.starred,
                    "reasons": i.reasons,
                    "tldr": i.tldr,
                    "actions": i.actions,
                    "open_questions": i.open_questions,
                    "notes": i.notes,
                }
                for i in items
            ],
        }


@router.post("/review/{source_id}/grade")
def grade(
    request: Request,
    source_id: int,
    value: str = Body(..., embed=True),
) -> dict[str, Any]:
    if value not in GRADES:
        raise HTTPException(400, f"反馈须为 {GRADES} 之一")
    deps = _deps(request)
    with deps.db() as db:
        if db.get_source(source_id) is None:
            raise HTTPException(404, f"找不到内容 #{source_id}")
        sched = ReviewScheduler(db, min_density=deps.config.review.min_density)
        interval, due = sched.grade(source_id, value)
        return {"interval_days": interval, "due_at": due, "stats": sched.stats()}


@router.post("/review/{source_id}/star")
def star(
    request: Request,
    source_id: int,
    starred: bool = Body(True, embed=True),
) -> dict[str, Any]:
    deps = _deps(request)
    with deps.db() as db:
        if db.get_source(source_id) is None:
            raise HTTPException(404, f"找不到内容 #{source_id}")
        ReviewScheduler(db, min_density=deps.config.review.min_density).set_starred(
            source_id, starred
        )
        return {"source_id": source_id, "starred": starred}


@router.post("/relations/{relation_id}/verify")
def verify_relation(
    request: Request,
    relation_id: int,
    accepted: bool = Body(..., embed=True),
) -> dict[str, Any]:
    """人工确认/否决 LLM 判定的关系。否决的不再参与图谱与简报。"""
    with _deps(request).db() as db:
        db.verify_relation(relation_id, accepted=accepted)
        return {"relation_id": relation_id, "accepted": accepted}


@router.get("/brief")
def brief(request: Request, days: int | None = Query(None)) -> dict[str, Any]:
    deps = _deps(request)
    with deps.db() as db:
        b = build_brief(db, days=days or deps.config.review.brief_days)
        return {
            "period_days": b.period_days,
            "since": b.since,
            "new_count": b.new_count,
            "total_chars": b.total_chars,
            "by_type": b.by_type,
            "top_content": b.top_content,
            "low_value": b.low_value,
            "contradictions": b.contradictions,
            "pending_actions": [
                {
                    "source_id": a.source_id,
                    "title": a.title,
                    "text": a.text,
                    "age_days": a.age_days,
                }
                for a in b.pending_actions
            ],
            "answered_questions": b.answered_questions,
            "open_questions": b.open_questions,
            "gaps": b.gaps,
            "due_reviews": b.due_reviews,
            "my_notes": b.my_notes,
            "hot_entities": b.hot_entities,
            "cost_usd": b.cost_usd,
            "is_empty": b.is_empty,
        }


@router.get("/gaps")
def gaps(request: Request, limit: int = Query(20, le=100)) -> dict[str, Any]:
    from ...knowledge import find_gaps

    with _deps(request).db() as db:
        return {
            "gaps": [
                {"name": g.name, "count": g.mention_count, "detail": g.detail}
                for g in find_gaps(db, limit=limit)
            ]
        }


@router.get("/questions")
def questions(request: Request, limit: int = Query(50, le=200)) -> dict[str, Any]:
    with _deps(request).db() as db:
        open_rows = db.open_questions(limit=limit)
        answered = db.conn.execute(
            "SELECT q.id, q.text, q.answered_by_source, s.title "
            "FROM questions q LEFT JOIN sources s ON s.id = q.answered_by_source "
            "WHERE q.status = 'answered' ORDER BY q.id DESC LIMIT 20"
        ).fetchall()
        return {
            "open": [
                {"id": int(r["id"]), "text": r["text"], "from_title": r["source_title"]}
                for r in open_rows
            ],
            "answered": [
                {
                    "id": int(r["id"]),
                    "text": r["text"],
                    "answer_id": r["answered_by_source"],
                    "answer_title": r["title"],
                }
                for r in answered
            ],
        }


__all__ = ["router"]
