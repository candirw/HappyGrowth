"""排序与平台能力补偿。

核心职责：当平台不支持某个排序指标时（如 B 站无收藏量排序），
拉取更大候选池 → 本地补齐指标 → 重排，并**明确标注这是近似值**。

不伪装精确是刻意的：用户需要知道「基于播放量 Top300 候选池的收藏量 Top100」
和「全站真实收藏量 Top100」不是一回事。
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Candidate, Query, RankBy


@dataclass
class RankResult:
    candidates: list[Candidate]
    notes: list[str]
    enriched: int = 0


_METRIC_GETTER = {
    RankBy.PLAY: lambda c: c.play_count,
    RankBy.FAVORITES: lambda c: c.favorite_count,
    RankBy.LIKES: lambda c: c.like_count,
    RankBy.COMMENTS: lambda c: c.comment_count,
}


def apply_filters(candidates: list[Candidate], query: Query) -> tuple[list[Candidate], list[str]]:
    """应用时间、时长、关键词过滤。"""
    notes: list[str] = []
    filters = query.filters
    kept: list[Candidate] = []
    dropped = {"date": 0, "duration": 0, "keyword": 0}

    for cand in candidates:
        if cand.excluded_reason:
            continue

        dr = filters.date_range
        if cand.published:
            if dr.from_date and cand.published < dr.from_date:
                dropped["date"] += 1
                continue
            if dr.to_date and cand.published > dr.to_date:
                dropped["date"] += 1
                continue

        d = filters.duration
        if cand.duration_sec is not None:
            if d.min_sec and cand.duration_sec < d.min_sec:
                dropped["duration"] += 1
                continue
            if d.max_sec and cand.duration_sec > d.max_sec:
                dropped["duration"] += 1
                continue

        title = cand.title
        if filters.exclude_keywords and any(
            kw and kw in title for kw in filters.exclude_keywords
        ):
            dropped["keyword"] += 1
            continue
        if filters.include_keywords and not any(
            kw and kw in title for kw in filters.include_keywords
        ):
            dropped["keyword"] += 1
            continue

        kept.append(cand)

    for reason, count in dropped.items():
        if count:
            label = {"date": "时间范围", "duration": "时长", "keyword": "关键词"}[reason]
            notes.append(f"{label}过滤掉 {count} 条")

    return kept, notes


def rank_candidates(
    candidates: list[Candidate],
    query: Query,
    *,
    provider=None,
) -> RankResult:
    """排序。平台不支持的指标在此本地补齐后重排。"""
    notes: list[str] = []
    enriched = 0
    metric = query.rank.by

    if metric is RankBy.DATE:
        ranked = sorted(
            candidates,
            key=lambda c: (c.published is not None, c.published),
            reverse=query.rank.desc,
        )
        return RankResult(ranked[: query.limit], notes, enriched)

    getter = _METRIC_GETTER.get(metric)
    if getter is None:
        # 相关度等：保持平台返回的原始顺序
        notes.append("按平台相关度排序（保持接口返回顺序）")
        return RankResult(candidates[: query.limit], notes, enriched)

    missing = [c for c in candidates if getter(c) is None]

    # 平台不支持该指标 → 本地补齐
    if missing and provider is not None and hasattr(provider, "enrich_favorites"):
        if metric in (RankBy.FAVORITES, RankBy.LIKES):
            # 只补足够重排的量，避免几百次请求触发风控
            budget = min(len(candidates), max(query.limit * 3, 60))
            enriched = provider.enrich_favorites(candidates, limit=budget)
            if enriched:
                notes.append(
                    f"平台不支持按该指标检索，已逐条补齐 {enriched} 条元数据后本地重排"
                )
                pool_desc = f"基于播放量 Top{budget} 候选池"
                notes.append(
                    f"⚠ 结果为**近似** Top{query.limit}（{pool_desc}），非全站精确排名"
                )

    ranked = sorted(
        candidates,
        key=lambda c: (getter(c) is not None, getter(c) or 0),
        reverse=query.rank.desc,
    )
    still_missing = sum(1 for c in ranked[: query.limit] if getter(c) is None)
    if still_missing:
        notes.append(f"{still_missing} 条缺少该指标数据，已排在末尾")

    return RankResult(ranked[: query.limit], notes, enriched)
