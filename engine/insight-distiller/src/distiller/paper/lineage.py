"""阅读路径与多篇对比。

`lineage` 解决的真实痛点：**论文看不懂常常是因为缺前置知识。**
按引文依赖与被引量排序，告诉你该先读哪篇。

`compare` 生成方法/数据集/指标对比表，用于横向评估多个方案。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .clients import AcademicClient, CitationRef, PaperMeta
from .resolve import PaperRef

ProgressFn = Callable[[str], None]


@dataclass
class ReadingStep:
    order: int
    title: str
    year: int | None
    citations: int
    url: str
    reason: str
    in_library: bool = False
    library_id: int | None = None


@dataclass
class Lineage:
    target: str
    steps: list[ReadingStep] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)


def build_lineage(
    client: AcademicClient,
    ref: PaperRef,
    meta: PaperMeta,
    *,
    limit: int = 8,
    db=None,
    progress: ProgressFn | None = None,
) -> Lineage:
    """构建阅读路径。

    排序依据（组合而非单一指标）：
    1. 被引量 —— 高被引的参考文献是领域共识，必须先懂
    2. 年份 —— 早发表的通常是更基础的工作
    结果是「先读奠基、再读目标论文」的路径。
    """
    progress = progress or (lambda _m: None)
    lineage = Lineage(target=meta.title or ref.describe())

    progress("拉取参考文献构建阅读路径")
    references = client.fetch_references(ref, limit=60)
    if not references:
        lineage.degraded.append("参考文献不可用，无法构建阅读路径")
        return lineage

    scored: list[tuple[CitationRef, float]] = []
    for r in references:
        citations = r.citation_count or 0
        if citations < 100:
            # 低被引的参考文献多为细节引用，不是前置知识
            continue
        # 被引量取对数避免单篇超高被引压倒一切；越早发表越优先
        import math

        score = math.log10(citations + 1) * 2.0
        if r.year and meta.year:
            gap = max(0, meta.year - r.year)
            score += min(gap, 10) * 0.15
        scored.append((r, score))

    if not scored:
        lineage.notes.append("参考文献均为低被引工作，无明显必读前置")
        return lineage

    scored.sort(key=lambda x: -x[1])
    for i, (r, _score) in enumerate(scored[:limit], start=1):
        step = ReadingStep(
            order=i,
            title=r.title,
            year=r.year,
            citations=r.citation_count or 0,
            url=r.url,
            reason=_lineage_reason(r, meta),
        )
        if db is not None and step.url:
            existing = db.find_source_by_url(step.url)
            if existing is not None:
                step.in_library = True
                step.library_id = existing.id
        lineage.steps.append(step)

    lineage.notes.append(
        f"从 {len(references)} 条参考文献中筛出 {len(lineage.steps)} 篇必读前置"
    )
    if client.degraded:
        lineage.degraded.extend(client.degraded)
    return lineage


def _lineage_reason(r: CitationRef, meta: PaperMeta) -> str:
    bits = []
    if (r.citation_count or 0) >= 10_000:
        bits.append("领域奠基工作")
    elif (r.citation_count or 0) >= 1000:
        bits.append("重要基础工作")
    else:
        bits.append("相关基础")
    if r.year and meta.year and meta.year > r.year:
        bits.append(f"早于本文 {meta.year - r.year} 年")
    return " · ".join(bits)


# ---------- 多篇对比 ----------

_COMPARE_SYSTEM = """你把多篇论文的精读结果整理成对比分析。

规则：
- **只使用给定的精读结果**，不补充外部知识，不推测。
- 某篇缺少某项信息时写「未提及」，不要编造。
- 指出各方案的**关键差异与适用场景差别**，这是对比的价值所在。
- 若多篇结论互相矛盾，必须明确指出分歧，不要自行裁决。
- 用简体中文，保留专业术语原文。只输出 JSON。"""

_COMPARE_TEMPLATE = """以下是 {n} 篇论文的精读结果：

{payloads}

输出 JSON：
{{
  "dimensions": ["对比维度，如 核心方法、数据集、关键指标、计算成本"],
  "rows": [
    {{"paper": "论文标题", "values": {{"维度名": "该论文在此维度的情况"}}}}
  ],
  "key_differences": ["各方案的本质差异，2-5条"],
  "contradictions": ["互相矛盾的结论，没有则空数组"],
  "recommendation": "在什么场景下该选哪个方案"
}}"""


def compare_papers(
    router,
    papers: list[tuple[str, dict[str, Any]]],
    *,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """对比多篇论文的精读结果。

    papers: [(标题, 论文 digest payload), ...]
    """
    import json
    import re

    progress = progress or (lambda _m: None)
    if len(papers) < 2:
        raise ValueError("至少需要 2 篇论文才能对比")

    # 只送对比必需的字段，避免 token 浪费
    condensed = []
    for title, payload in papers:
        condensed.append(
            {
                "title": title,
                "problem": payload.get("problem", ""),
                "method": payload.get("method", {}),
                "experiments": payload.get("experiments", {}),
                "conclusion": payload.get("conclusion", []),
                "limitations": payload.get("limitations", []),
            }
        )

    progress(f"对比 {len(papers)} 篇论文")
    resp = router.complete(
        "reduce",
        _COMPARE_TEMPLATE.format(
            n=len(papers),
            payloads=json.dumps(condensed, ensure_ascii=False, indent=1),
        ),
        system=_COMPARE_SYSTEM,
        json_mode=True,
    )

    text = (resp.text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        data = {}
        if start != -1:
            depth = 0
            for i, ch in enumerate(text[start:], start=start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            data = json.loads(text[start : i + 1])
                        except json.JSONDecodeError:
                            data = {}
                        break

    data["model_used"] = f"{resp.provider}/{resp.model}"
    data["cost_usd"] = router.total_cost
    return data
