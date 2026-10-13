"""Web 层数据装配。

把 store 层的 Row / Pydantic 模型转成模板与 JSON 都能直接用的字典，
**并在这里完成深链计算**——模板里不该有业务逻辑，前端也不该重复实现
一套平台判断。
"""

from __future__ import annotations

import json
from typing import Any

from ..models import Digest, Source
from ..store import Database
from .deeplink import deeplink, format_seconds, supports_timestamp

# 密度分档。与 CLI 的判定口径保持一致，避免两端结论不同。
DENSITY_BANDS = (
    (65.0, "high", "值得读原文"),
    (40.0, "mid", "看浓缩版就够"),
    (0.0, "low", "疑似水文"),
)

REL_LABELS = {
    "cites": ("引用", "→"),
    "cited_by": ("被引用", "←"),
    "contradicts": ("结论冲突", "⚠"),
    "supports": ("互相印证", "✓"),
    "extends": ("延伸补充", "+"),
    "same_topic": ("同主题", "≈"),
    "answers": ("回答了疑问", "?"),
    "prerequisite": ("前置知识", "⊂"),
}


def density_band(score: float | None) -> dict[str, Any]:
    """密度分 → 视觉分档。None 表示未评分（不等于低分）。"""
    if score is None:
        return {"score": None, "level": "none", "verdict": "未评分", "pct": 0}
    for floor, level, verdict in DENSITY_BANDS:
        if score >= floor:
            return {
                "score": round(score, 1),
                "level": level,
                "verdict": verdict,
                "pct": max(0, min(100, int(score))),
            }
    return {"score": score, "level": "low", "verdict": "疑似水文", "pct": 0}


def source_brief(src: Source) -> dict[str, Any]:
    """列表页用的精简表示。"""
    return {
        "id": src.id,
        "title": src.title,
        "url": src.url,
        "platform": src.platform,
        "content_type": src.content_type.value,
        "author": src.author,
        "status": src.status.value if hasattr(src.status, "value") else str(src.status),
        "publish_at": src.publish_at.date().isoformat() if src.publish_at else None,
        "duration": format_seconds(src.duration_sec) if src.duration_sec else None,
        "domains": list(src.domains or []),
        "density": density_band(src.density_score),
        "created_at": (src.created_at.isoformat() if src.created_at else None),
    }


def _anchor_link(src: Source, anchor: str | None) -> dict[str, Any]:
    """锚点 → 可跳转结构。不能定位时 jumpable=False，前端渲染为纯文本。"""
    if not anchor:
        return {"text": None, "url": None, "jumpable": False}
    url = deeplink(src.url, platform=src.platform, anchor=anchor)
    return {
        "text": anchor,
        "url": url,
        # 只有真的带上了时间偏移才算可跳转
        "jumpable": url != src.url and supports_timestamp(src.platform),
    }


def digest_view(src: Source, digest: Digest) -> dict[str, Any]:
    """阅读页的分层浓缩结构，锚点已转成链接。"""
    return {
        "tldr": list(digest.tldr or []),
        "contrarian": list(digest.contrarian or []),
        "outline": [
            {
                "title": item.title,
                "points": list(item.points or []),
                "anchor": _anchor_link(src, item.anchor),
            }
            for item in (digest.outline or [])
        ],
        "concepts": [
            {"term": c.term, "definition": c.definition} for c in (digest.concepts or [])
        ],
        "quotes": [
            {"text": q.text, "anchor": _anchor_link(src, q.anchor)}
            for q in (digest.quotes or [])
        ],
        "actions": list(digest.actions or []),
        "open_questions": list(digest.open_questions or []),
        "density": density_band(digest.density_score),
        "density_verdict": digest.density_verdict,
        "model": digest.model_used,
        "tokens": f"{digest.tokens_in}/{digest.tokens_out}",
        "cost": round(digest.cost_usd or 0.0, 4),
    }


def relations_view(db: Database, source_id: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in db.get_relations(source_id, limit=40):
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except json.JSONDecodeError:
            evidence = {}
        label, icon = REL_LABELS.get(row["rel_type"], (row["rel_type"], "·"))
        out.append(
            {
                "id": int(row["other_id"]),
                "title": row["other_title"],
                "rel_type": row["rel_type"],
                "label": label,
                "icon": icon,
                "confidence": round(float(row["confidence"] or 0), 2),
                "note": evidence.get("note"),
                "new_claim": evidence.get("new_claim"),
                "old_claim": evidence.get("old_claim"),
                # 冲突要突出显示——这是认知升级点
                "is_conflict": row["rel_type"] == "contradicts",
            }
        )
    out.sort(key=lambda r: (not r["is_conflict"], -r["confidence"]))
    return out


def segments_view(db: Database, src: Source) -> list[dict[str, Any]]:
    """原文片段，每段带跳转链接。"""
    out: list[dict[str, Any]] = []
    for seg in db.get_segments(int(src.id)):
        seconds = seg.start_ms // 1000 if seg.start_ms is not None else None
        url = (
            deeplink(src.url, platform=src.platform, seconds=seconds)
            if seconds is not None
            else src.url
        )
        out.append(
            {
                "idx": seg.idx,
                "text": seg.text,
                "anchor": seg.anchor,
                "url": url,
                "jumpable": seconds is not None and supports_timestamp(src.platform),
                "section": seg.section,
            }
        )
    return out


def notes_view(db: Database, source_id: int) -> list[dict[str, Any]]:
    return [
        {
            "id": int(r["id"]),
            "text": r["text"],
            "note_type": r["note_type"],
            "created_at": (r["created_at"] or "")[:16],
        }
        for r in db.get_notes(source_id)
    ]


def hits_view(hits: list) -> list[dict[str, Any]]:
    """检索命中 → 前端卡片。保留召回路径，让人看出混合检索的作用。"""
    out: list[dict[str, Any]] = []
    for h in hits:
        routes = []
        if getattr(h, "vector_rank", None):
            routes.append(f"语义#{h.vector_rank}")
        if getattr(h, "keyword_rank", None):
            routes.append(f"关键词#{h.keyword_rank}")
        out.append(
            {
                "source_id": h.source_id,
                "title": h.title,
                "url": h.url,
                "text": h.text,
                "anchor": h.anchor,
                "score": round(float(h.score or 0), 4),
                "routes": routes,
            }
        )
    return out


__all__ = [
    "DENSITY_BANDS",
    "REL_LABELS",
    "density_band",
    "digest_view",
    "hits_view",
    "notes_view",
    "relations_view",
    "segments_view",
    "source_brief",
]
