"""相似论文检索：四路召回 + RRF 融合 + 分组展示。

**四路（已按实测调整，见 DECISIONS.md）**：
1. 参考文献 → 奠基工作（确定性，最可靠）
2. 被引论文 → 后续进展（确定性）
3. 共被引 / 文献耦合 → 同期竞品（几十年验证的学术相似度指标）
4. 语义 + 关键词 → 跨领域启发（唯一能发现引文无交集但方法相通的路）

原方案的 S2 Recommendations 一路已移除——实测稳定返回空列表，
保留只会让用户误以为功能正常。

**分组而非平铺**：平铺列表无法体现学术脉络。
「跨领域启发」组只有语义路能发现，是新想法的主要来源。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .clients import AcademicClient, CitationRef, PaperMeta
from .resolve import PaperRef

ProgressFn = Callable[[str], None]

# 分组定义
GROUP_FOUNDATION = "奠基工作"
GROUP_COMPETITOR = "同期竞品"
GROUP_FOLLOWUP = "后续进展"
GROUP_CROSSOVER = "跨领域启发"

_GROUP_ORDER = (
    GROUP_FOUNDATION,
    GROUP_COMPETITOR,
    GROUP_FOLLOWUP,
    GROUP_CROSSOVER,
)

_GROUP_HINT = {
    GROUP_FOUNDATION: "本文引用且高被引 —— 建议先补这些",
    GROUP_COMPETITOR: "与本文引用重叠度高，很可能在解决同类问题",
    GROUP_FOLLOWUP: "引用了本文，可能已有改进",
    GROUP_CROSSOVER: "语义相近但引文无交集 —— 最容易漏掉的价值点",
}


@dataclass
class SimilarPaper:
    ref: CitationRef
    group: str
    score: float = 0.0
    routes: list[str] = field(default_factory=list)
    in_library: bool = False
    library_id: int | None = None
    reason: str = ""

    @property
    def title(self) -> str:
        return self.ref.title

    @property
    def citations(self) -> int:
        return self.ref.citation_count or 0


@dataclass
class SimilarResult:
    groups: dict[str, list[SimilarPaper]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.groups.values())

    def ordered(self) -> list[tuple[str, str, list[SimilarPaper]]]:
        """按固定顺序返回非空分组：(组名, 说明, 论文列表)"""
        return [
            (name, _GROUP_HINT[name], self.groups[name])
            for name in _GROUP_ORDER
            if self.groups.get(name)
        ]

    def flat(self) -> list[SimilarPaper]:
        out: list[SimilarPaper] = []
        for name in _GROUP_ORDER:
            out.extend(self.groups.get(name, []))
        return out


def _key(ref: CitationRef) -> str:
    """去重键。同一篇论文可能来自不同路，需合并。"""
    if ref.arxiv_id:
        return f"arxiv:{ref.arxiv_id}"
    if ref.doi:
        return f"doi:{ref.doi.lower()}"
    if ref.s2_id:
        return f"s2:{ref.s2_id}"
    return f"title:{ref.title.lower()[:60]}"


def _impact_label(citations: int | None) -> str:
    """影响力标签。比裸数字更有决策价值。"""
    n = citations or 0
    if n >= 10_000:
        return f"被引 {n}（领域奠基）"
    if n >= 1000:
        return f"被引 {n}（重要工作）"
    if n >= 100:
        return f"被引 {n}"
    if n > 0:
        return f"被引 {n}（影响有限）"
    return "尚无被引"


class SimilarFinder:
    def __init__(
        self,
        client: AcademicClient,
        *,
        progress: ProgressFn | None = None,
        db=None,
    ) -> None:
        self.client = client
        self.progress = progress or (lambda _m: None)
        self.db = db

    def find(
        self,
        ref: PaperRef,
        meta: PaperMeta,
        *,
        limit_per_group: int = 5,
        strategies: set[str] | None = None,
        unread_only: bool = False,
    ) -> SimilarResult:
        """四路召回并分组。任一路失败都降级而非整体失败。"""
        strategies = strategies or {"citation", "coupling", "semantic"}
        result = SimilarResult()

        references: list[CitationRef] = []
        citations: list[CitationRef] = []

        # ---- 路 1&2：引文图谱（确定性，最可靠） ----
        if "citation" in strategies:
            self.progress("拉取参考文献（奠基工作）")
            references = self.client.fetch_references(ref, limit=60)
            if references:
                result.notes.append(f"参考文献 {len(references)} 条")
            else:
                result.degraded.append("参考文献不可用")

            self.progress("拉取被引论文（后续进展）")
            citations = self.client.fetch_citations(ref, limit=60)
            if citations:
                result.notes.append(f"被引论文 {len(citations)} 条")
            else:
                result.degraded.append("被引列表不可用")

        # ---- 路 3：文献耦合（同期竞品） ----
        coupling: dict[str, tuple[CitationRef, int]] = {}
        if "coupling" in strategies and references:
            coupling = self._bibliographic_coupling(references, result)

        # ---- 路 4：语义/关键词（跨领域启发） ----
        crossover: list[CitationRef] = []
        if "semantic" in strategies:
            crossover = self._semantic_search(meta, result)

        # ---- 组装分组 ----
        seen: set[str] = {_key(CitationRef(title=meta.title or "", arxiv_id=meta.arxiv_id))}

        def take(
            items: list[tuple[CitationRef, float, str]], group: str
        ) -> list[SimilarPaper]:
            picked: list[SimilarPaper] = []
            for cand, score, reason in items:
                k = _key(cand)
                if k in seen or not cand.title:
                    continue
                seen.add(k)
                paper = SimilarPaper(
                    ref=cand, group=group, score=score, reason=reason
                )
                self._mark_library(paper)
                if unread_only and paper.in_library:
                    continue
                picked.append(paper)
                if len(picked) >= limit_per_group:
                    break
            return picked

        # 奠基工作：按被引数排序（高被引的参考文献才是真正的奠基）
        foundation = sorted(references, key=lambda c: -(c.citation_count or 0))
        result.groups[GROUP_FOUNDATION] = take(
            [
                (c, float(c.citation_count or 0), _impact_label(c.citation_count))
                for c in foundation
            ],
            GROUP_FOUNDATION,
        )

        # 同期竞品：文献耦合强度优先
        competitor_items = sorted(
            coupling.values(), key=lambda x: (-x[1], -(x[0].citation_count or 0))
        )
        result.groups[GROUP_COMPETITOR] = take(
            [
                (c, float(shared), f"共同引用 {shared} 篇文献")
                for c, shared in competitor_items
            ],
            GROUP_COMPETITOR,
        )

        # 后续进展：被引数与时效性结合排序。
        # 纯按被引会全是老论文（新论文还没被引），
        # 纯按时间会全是刚发布的低质量工作。
        # 用「年均被引」平衡：既认可影响力，也不歧视新工作。
        import datetime

        this_year = datetime.date.today().year

        def followup_score(c: CitationRef) -> float:
            citations = c.citation_count or 0
            age = max(1, this_year - (c.year or this_year) + 1)
            return citations / age

        followup = sorted(citations, key=lambda c: -followup_score(c))
        result.groups[GROUP_FOLLOWUP] = take(
            [
                (
                    c,
                    followup_score(c),
                    f"被引 {c.citation_count or 0}"
                    + (f" · {c.year}" if c.year else ""),
                )
                for c in followup
            ],
            GROUP_FOLLOWUP,
        )

        # 跨领域：语义相近但不在引文网络里
        result.groups[GROUP_CROSSOVER] = take(
            [(c, float(c.citation_count or 0), "语义相近") for c in crossover],
            GROUP_CROSSOVER,
        )

        if self.client.degraded:
            result.degraded.extend(self.client.degraded)
        return result

    # ---------- 各路实现 ----------

    def _bibliographic_coupling(
        self, references: list[CitationRef], result: SimilarResult
    ) -> dict[str, tuple[CitationRef, int]]:
        """文献耦合：找出同样引用了这些参考文献的其他论文。

        原理：两篇论文引用的参考文献重叠越多，越可能在解决同类问题。
        这是几十年验证的学术相似度指标，比向量相似度可靠。

        **关键细节**：必须排除超高被引的通用基础论文（Attention、Adam、BERT 等）。
        几乎所有 ML 论文都引用它们，用它们做耦合会召回大量完全不相关的工作
        （实测曾召回物理、跨语言对齐等无关论文）。
        真正有区分度的是**中等被引的领域内论文**。
        """
        usable = [r for r in references if (r.arxiv_id or r.doi or r.s2_id)]
        if not usable:
            result.degraded.append("文献耦合：参考文献缺少可查询标识")
            return {}

        # 通用基础论文阈值：被引超过此值视为"所有人都引"，无区分度
        GENERIC_THRESHOLD = 20_000
        specific = [
            r for r in usable
            if r.citation_count is not None
            and 50 <= r.citation_count <= GENERIC_THRESHOLD
        ]
        excluded = [
            r for r in usable
            if r.citation_count is not None and r.citation_count > GENERIC_THRESHOLD
        ]

        if excluded:
            result.notes.append(
                f"文献耦合已排除 {len(excluded)} 篇通用基础论文"
                f"（如《{excluded[0].title[:28]}》，被引过高无区分度）"
            )

        # 优先中等被引且较新的论文——领域特异性最强
        pool = sorted(
            specific,
            key=lambda c: (-(c.year or 0), -(c.citation_count or 0)),
        )[:4]

        if not pool:
            result.degraded.append(
                "文献耦合：参考文献均为通用基础论文或缺少被引数，跳过该路"
            )
            return {}

        self.progress(f"计算文献耦合（反查 {len(pool)} 篇领域内参考文献）")
        counter: dict[str, tuple[CitationRef, int]] = {}
        succeeded = 0

        for r in pool:
            sub_ref = PaperRef(arxiv_id=r.arxiv_id, doi=r.doi, s2_id=r.s2_id)
            citing = self.client.fetch_citations(sub_ref, limit=40)
            if not citing:
                continue
            succeeded += 1
            for c in citing:
                k = _key(c)
                if k in counter:
                    existing, count = counter[k]
                    counter[k] = (existing, count + 1)
                else:
                    counter[k] = (c, 1)

        if not succeeded:
            result.degraded.append("文献耦合：反查全部失败（限流）")
            return {}

        # 只保留至少共同引用 2 篇的；单篇重叠说服力不足
        filtered = {k: v for k, v in counter.items() if v[1] >= 2}
        if filtered:
            result.notes.append(
                f"文献耦合：{len(filtered)} 篇与本文有 2+ 篇共同参考文献"
            )
            return filtered

        # 退化情形：只有单篇重叠时，要求对方本身有一定影响力，
        # 否则会引入大量零被引的无关论文
        fallback = {
            k: v for k, v in counter.items()
            if (v[0].citation_count or 0) >= 10
        }
        if fallback:
            result.notes.append(
                f"文献耦合：无 2+ 重叠，退化为单篇重叠且被引≥10 的 {len(fallback)} 篇"
            )
        return fallback

    def _semantic_search(
        self, meta: PaperMeta, result: SimilarResult
    ) -> list[CitationRef]:
        """关键词检索找语义相近的论文。

        用标题的核心词而非整句——整句检索召回率很低。
        """
        query = _build_query(meta)
        if not query:
            return []
        self.progress(f"语义检索「{query}」")
        found = self.client.search_s2(query, limit=12)
        if found:
            result.notes.append(f"语义检索命中 {len(found)} 条")
        else:
            result.degraded.append("语义检索不可用")
        return found

    def _mark_library(self, paper: SimilarPaper) -> None:
        """标注已在库中，避免重复消化花钱。"""
        if self.db is None:
            return
        url = paper.ref.url
        if not url:
            return
        existing = self.db.find_source_by_url(url)
        if existing is not None:
            paper.in_library = True
            paper.library_id = existing.id


_STOPWORDS = {
    "a", "an", "the", "of", "for", "with", "and", "or", "in", "on", "to",
    "is", "are", "using", "via", "towards", "toward", "all", "you", "need",
    "we", "our", "new", "novel", "efficient", "effective", "learning",
}


def _build_query(meta: PaperMeta) -> str:
    """从标题提取检索词。保留专有名词与技术术语。"""
    import re

    title = meta.title or ""
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-]+", title)
    kept = [w for w in words if w.lower() not in _STOPWORDS and len(w) > 2]
    if not kept:
        kept = words[:4]
    return " ".join(kept[:6])
