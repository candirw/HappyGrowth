"""学术 API 客户端。

**分工（基于实测，见 DECISIONS.md）**：
- arXiv：最稳定、无限流 → 元数据与全文主力
- Semantic Scholar：引文图谱（references / citations）主力，需退避重试
- OpenAlex：仅单篇查询作补充（list 类已需付费）
- Crossref：DOI 元数据兜底

所有请求走缓存 + 指数退避；任一源失败都降级而非整体失败。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .cache import ApiCache
from .resolve import PaperRef

ProgressFn = Callable[[str], None]

_ARXIV_API = "https://export.arxiv.org/api/query"
_S2_API = "https://api.semanticscholar.org/graph/v1"
_OPENALEX_API = "https://api.openalex.org"
_CROSSREF_API = "https://api.crossref.org/works"

# arXiv 官方建议每次请求间隔 3 秒
_ARXIV_MIN_INTERVAL = 3.0
_S2_MIN_INTERVAL = 1.2


@dataclass
class PaperMeta:
    """论文元数据。字段可能部分缺失，取决于哪些源可用。"""

    title: str = ""
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    published: str | None = None
    venue: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    s2_id: str | None = None
    citation_count: int | None = None
    reference_count: int | None = None
    # None 表示「未能确认」，不是 False。
    # 限流时若默认 False/True 都会给出错误的可信度结论。
    is_preprint: bool | None = None
    pdf_url: str | None = None
    categories: list[str] = field(default_factory=list)
    sources_used: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def publication_status(self) -> str:
        """发表状态的人类可读描述。不确定时如实说明。"""
        if self.is_preprint is None:
            return "发表状态未知"
        return "预印本" if self.is_preprint else "已正式发表"

    @property
    def credibility(self) -> dict[str, Any]:
        """可信度信息。论文特有——预印本与顶会论文不能等同呈现。

        peer_reviewed 用三态：True / False / None（未确认），
        避免因数据源限流而误报「未经同行评审」。
        """
        peer_reviewed: bool | None
        if self.venue:
            peer_reviewed = not bool(self.is_preprint)
        elif self.is_preprint is None:
            peer_reviewed = None
        else:
            peer_reviewed = False

        return {
            "venue": self.venue,
            "citations": self.citation_count,
            "is_preprint": self.is_preprint,
            "peer_reviewed": peer_reviewed,
            "status": self.publication_status,
            "year": self.year,
        }


@dataclass
class CitationRef:
    """引文条目（参考文献或被引论文）。"""

    title: str
    s2_id: str | None = None
    arxiv_id: str | None = None
    doi: str | None = None
    year: int | None = None
    citation_count: int | None = None

    @property
    def url(self) -> str:
        if self.arxiv_id:
            return f"https://arxiv.org/abs/{self.arxiv_id}"
        if self.doi:
            return f"https://doi.org/{self.doi}"
        if self.s2_id:
            return f"https://www.semanticscholar.org/paper/{self.s2_id}"
        return ""


class AcademicClient:
    def __init__(
        self,
        cache: ApiCache,
        *,
        mailto: str | None = None,
        progress: ProgressFn | None = None,
        timeout: int = 30,
        max_retries: int = 4,
    ) -> None:
        self.cache = cache
        self.mailto = mailto or "insight-distiller@example.com"
        self.progress = progress or (lambda _m: None)
        self.timeout = timeout
        self.max_retries = max_retries
        self._last_call: dict[str, float] = {}
        self.degraded: list[str] = []

    # ---------- 底层请求 ----------

    def _throttle(self, source: str, min_interval: float) -> None:
        last = self._last_call.get(source, 0.0)
        gap = time.monotonic() - last
        if gap < min_interval:
            time.sleep(min_interval - gap)
        self._last_call[source] = time.monotonic()

    def _request(
        self,
        source: str,
        url: str,
        params: dict,
        *,
        min_interval: float,
        cache_key: str | None = None,
        as_json: bool = True,
    ) -> Any | None:
        """带缓存与指数退避的请求。失败返回 None 并记录降级。"""
        if cache_key:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        headers = {"User-Agent": f"insight-distiller (mailto:{self.mailto})"}
        for attempt in range(self.max_retries):
            self._throttle(source, min_interval)
            try:
                resp = httpx.get(
                    url, params=params, headers=headers,
                    timeout=self.timeout, follow_redirects=True,
                )
            except httpx.RequestError as exc:
                if attempt == self.max_retries - 1:
                    self._degrade(source, f"网络错误 {exc}")
                    return None
                time.sleep(1.5 * (attempt + 1))
                continue

            if resp.status_code == 200:
                data = resp.json() if as_json else resp.text
                if cache_key:
                    self.cache.set(cache_key, data)
                return data

            if resp.status_code == 429:
                # 匿名共享池限流：退避重试
                wait = 2.0 * (attempt + 1)
                if attempt < self.max_retries - 1:
                    time.sleep(wait)
                    continue
                self._degrade(source, "限流（429），已重试多次仍失败")
                return None

            if resp.status_code == 404:
                return None

            if attempt == self.max_retries - 1:
                self._degrade(source, f"HTTP {resp.status_code}")
                return None
            time.sleep(1.5 * (attempt + 1))
        return None

    def _degrade(self, source: str, reason: str) -> None:
        note = f"{source}: {reason}"
        if note not in self.degraded:
            self.degraded.append(note)
            self.progress(f"[降级] {note}")

    # ---------- arXiv（主力，无限流） ----------

    def fetch_arxiv(self, arxiv_id: str) -> PaperMeta | None:
        text = self._request(
            "arxiv", _ARXIV_API, {"id_list": arxiv_id},
            min_interval=_ARXIV_MIN_INTERVAL,
            cache_key=f"arxiv:meta:{arxiv_id}",
            as_json=False,
        )
        if not text or "<entry>" not in text:
            return None
        return _parse_arxiv_entry(text, arxiv_id)

    def search_arxiv(self, title: str, *, limit: int = 5) -> list[PaperMeta]:
        """按标题搜索。用于只给了标题的情况。"""
        cleaned = re.sub(r"[^\w\s\u4e00-\u9fff-]", " ", title).strip()
        text = self._request(
            "arxiv", _ARXIV_API,
            {"search_query": f'ti:"{cleaned}"', "max_results": limit},
            min_interval=_ARXIV_MIN_INTERVAL,
            cache_key=f"arxiv:search:{cleaned.lower()[:80]}",
            as_json=False,
        )
        if not text:
            return []
        return _parse_arxiv_feed(text)

    # ---------- Semantic Scholar（引文主力） ----------

    def fetch_s2(self, ref: PaperRef) -> PaperMeta | None:
        lookup = ref.s2_lookup_id
        if not lookup:
            return None
        data = self._request(
            "s2", f"{_S2_API}/paper/{lookup}",
            {
                "fields": "title,abstract,year,venue,citationCount,referenceCount,"
                          "externalIds,authors.name,openAccessPdf,publicationTypes"
            },
            min_interval=_S2_MIN_INTERVAL,
            cache_key=f"s2:meta:{lookup.lower()}",
        )
        if not data:
            return None
        return _parse_s2_paper(data)

    def fetch_references(self, ref: PaperRef, *, limit: int = 60) -> list[CitationRef]:
        """参考文献 → 「奠基工作」召回路。"""
        lookup = ref.s2_lookup_id
        if not lookup:
            return []
        data = self._request(
            "s2", f"{_S2_API}/paper/{lookup}/references",
            {"fields": "title,year,citationCount,externalIds", "limit": limit},
            min_interval=_S2_MIN_INTERVAL,
            cache_key=f"s2:refs:{lookup.lower()}:{limit}",
        )
        if not data:
            return []
        return [
            c for c in (
                _parse_citation(item.get("citedPaper") or {})
                for item in (data.get("data") or [])
            ) if c
        ]

    def fetch_citations(self, ref: PaperRef, *, limit: int = 60) -> list[CitationRef]:
        """被引论文 → 「后续进展」召回路。"""
        lookup = ref.s2_lookup_id
        if not lookup:
            return []
        data = self._request(
            "s2", f"{_S2_API}/paper/{lookup}/citations",
            {"fields": "title,year,citationCount,externalIds", "limit": limit},
            min_interval=_S2_MIN_INTERVAL,
            cache_key=f"s2:cites:{lookup.lower()}:{limit}",
        )
        if not data:
            return []
        return [
            c for c in (
                _parse_citation(item.get("citingPaper") or {})
                for item in (data.get("data") or [])
            ) if c
        ]

    def search_s2(self, query: str, *, limit: int = 10) -> list[CitationRef]:
        """关键词检索 → 「跨领域/相关」召回路的补充。"""
        data = self._request(
            "s2", f"{_S2_API}/paper/search",
            {"query": query, "fields": "title,year,citationCount,externalIds", "limit": limit},
            min_interval=_S2_MIN_INTERVAL,
            cache_key=f"s2:search:{query.lower()[:80]}:{limit}",
        )
        if not data:
            return []
        return [
            c for c in (_parse_citation(p) for p in (data.get("data") or [])) if c
        ]

    # ---------- OpenAlex（仅单篇，list 类已需付费） ----------

    def fetch_openalex(self, ref: PaperRef) -> dict | None:
        """单篇查询。实测可用；但按 DOI 过滤等 list 请求会 429。"""
        oa_id = ref.openalex_id
        if not oa_id and ref.doi:
            # 直接用 DOI 路径形式（属单篇查询，不是 filter）
            oa_id = f"doi:{ref.doi}"
        if not oa_id:
            return None
        return self._request(
            "openalex", f"{_OPENALEX_API}/works/{oa_id}",
            {"mailto": self.mailto},
            min_interval=1.0,
            cache_key=f"oa:work:{oa_id.lower()}",
        )

    # ---------- 统一入口 ----------

    def resolve_meta(self, ref: PaperRef) -> PaperMeta:
        """多源融合取元数据。arXiv 优先，S2 补引用数，OpenAlex 兜底。"""
        meta: PaperMeta | None = None

        if ref.arxiv_id:
            self.progress(f"查询 arXiv:{ref.arxiv_id}")
            meta = self.fetch_arxiv(ref.arxiv_id)
            if meta:
                meta.sources_used.append("arxiv")

        if meta is None and ref.title:
            self.progress("按标题搜索 arXiv")
            results = self.search_arxiv(ref.title, limit=3)
            if results:
                meta = results[0]
                meta.sources_used.append("arxiv-search")
                ref.arxiv_id = meta.arxiv_id

        if meta is None:
            self.progress("查询 Semantic Scholar")
            meta = self.fetch_s2(ref)
            if meta:
                meta.sources_used.append("s2")

        if meta is None:
            oa = self.fetch_openalex(ref)
            if oa:
                meta = _parse_openalex(oa)
                meta.sources_used.append("openalex")

        if meta is None:
            meta = PaperMeta(title=ref.title or ref.describe())
            meta.warnings.append(
                "所有学术数据源均未返回元数据"
                + (f"（{'; '.join(self.degraded)}）" if self.degraded else "")
            )
            return meta

        # 补引用数与发表信息（arXiv 不提供这些）
        if meta.citation_count is None or not meta.venue:
            s2 = self.fetch_s2(ref)
            if s2:
                if meta.citation_count is None:
                    meta.citation_count = s2.citation_count
                    meta.reference_count = s2.reference_count
                if not meta.venue and s2.venue:
                    meta.venue = s2.venue
                    # 关键修正：arXiv 元数据无 journal_ref 时会默认判为预印本，
                    # 但 S2 若给出了 venue（如 NeurIPS），说明已正式发表。
                    # 不修正会让顶会论文显示为「预印本」，误导可信度判断。
                    meta.is_preprint = False
                if "s2" not in meta.sources_used:
                    meta.sources_used.append("s2")

        if self.degraded:
            meta.warnings.extend(f"数据源降级 · {d}" for d in self.degraded)
        return meta


# ---------- 解析函数 ----------


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _parse_arxiv_entry(xml: str, arxiv_id: str) -> PaperMeta | None:
    entry_start = xml.find("<entry>")
    if entry_start == -1:
        return None
    entry = xml[entry_start : xml.find("</entry>", entry_start)]

    title_m = re.search(r"<title>(.*?)</title>", entry, re.S)
    summary_m = re.search(r"<summary>(.*?)</summary>", entry, re.S)
    published_m = re.search(r"<published>([^<]+)</published>", entry)
    doi_m = re.search(r"<arxiv:doi[^>]*>([^<]+)</arxiv:doi>", entry)
    journal_m = re.search(r"<arxiv:journal_ref[^>]*>([^<]+)</arxiv:journal_ref>", entry)

    published = published_m.group(1) if published_m else None
    year = int(published[:4]) if published and published[:4].isdigit() else None

    pdf_url = None
    if m := re.search(r'<link[^>]+title="pdf"[^>]+href="([^"]+)"', entry):
        pdf_url = m.group(1)
    elif m := re.search(r'<link[^>]+href="([^"]+)"[^>]+title="pdf"', entry):
        pdf_url = m.group(1)
    if not pdf_url:
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"

    return PaperMeta(
        title=_clean(title_m.group(1)) if title_m else "",
        abstract=_clean(summary_m.group(1)) if summary_m else "",
        authors=[_clean(a) for a in re.findall(r"<name>([^<]+)</name>", entry)],
        year=year,
        published=published,
        venue=_clean(journal_m.group(1)) if journal_m else None,
        doi=_clean(doi_m.group(1)) if doi_m else None,
        arxiv_id=arxiv_id,
        # 有 journal_ref 才能确认已发表；没有则**未知**（不能断言是预印本，
        # 因为 arXiv 元数据常年不更新 journal_ref，需靠 S2 补充确认）
        is_preprint=False if journal_m else None,
        pdf_url=pdf_url,
        categories=re.findall(r'<category[^>]+term="([^"]+)"', entry),
    )


def _parse_arxiv_feed(xml: str) -> list[PaperMeta]:
    metas: list[PaperMeta] = []
    for chunk in xml.split("<entry>")[1:]:
        entry = "<entry>" + chunk
        id_m = re.search(r"arxiv\.org/abs/([0-9]{4}\.[0-9]{4,5})", entry)
        if not id_m:
            continue
        meta = _parse_arxiv_entry(entry, id_m.group(1))
        if meta and meta.title:
            metas.append(meta)
    return metas


def _parse_s2_paper(data: dict) -> PaperMeta:
    ext = data.get("externalIds") or {}
    pub_types = data.get("publicationTypes") or []
    pdf = data.get("openAccessPdf") or {}

    # 只在 S2 明确给出 publicationTypes 时判定，否则留 None
    is_preprint: bool | None = None
    if pub_types:
        is_preprint = not any(
            t in pub_types for t in ("JournalArticle", "Conference")
        )
    elif data.get("venue"):
        is_preprint = False

    return PaperMeta(
        title=_clean(data.get("title") or ""),
        abstract=_clean(data.get("abstract") or ""),
        authors=[a.get("name", "") for a in (data.get("authors") or [])],
        year=data.get("year"),
        venue=data.get("venue") or None,
        doi=ext.get("DOI"),
        arxiv_id=ext.get("ArXiv"),
        s2_id=data.get("paperId"),
        citation_count=data.get("citationCount"),
        reference_count=data.get("referenceCount"),
        is_preprint=is_preprint,
        pdf_url=pdf.get("url"),
    )


def _parse_citation(data: dict) -> CitationRef | None:
    title = _clean(data.get("title") or "")
    if not title:
        return None
    ext = data.get("externalIds") or {}
    return CitationRef(
        title=title,
        s2_id=data.get("paperId"),
        arxiv_id=ext.get("ArXiv"),
        doi=ext.get("DOI"),
        year=data.get("year"),
        citation_count=data.get("citationCount"),
    )


def _parse_openalex(data: dict) -> PaperMeta:
    ids = data.get("ids") or {}
    loc = data.get("primary_location") or {}
    src = loc.get("source") or {}
    return PaperMeta(
        title=_clean(data.get("title") or ""),
        abstract=_clean((data.get("abstract_inverted_index") and "") or ""),
        authors=[
            (a.get("author") or {}).get("display_name", "")
            for a in (data.get("authorships") or [])
        ],
        year=data.get("publication_year"),
        venue=src.get("display_name"),
        doi=(ids.get("doi") or "").replace("https://doi.org/", "") or None,
        citation_count=data.get("cited_by_count"),
        reference_count=len(data.get("referenced_works") or []),
        is_preprint=(data.get("type") == "preprint") if data.get("type") else None,
        categories=[
            c.get("display_name", "") for c in (data.get("concepts") or [])[:6]
        ],
    )
