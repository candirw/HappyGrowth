"""论文模块测试（M3）。

网络相关部分用 stub client，保证离线可跑、秒级完成。
重点覆盖：标识解析、分节抽取、可信度三态、文献耦合的通用论文排除。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from distiller.paper.clients import CitationRef, PaperMeta
from distiller.paper.lineage import build_lineage
from distiller.paper.parse import (
    PRIORITY_SECTIONS,
    SKIP_SECTIONS,
    _detect_section,
    _looks_like_reference,
    _split_sections,
)
from distiller.paper.resolve import parse_ref
from distiller.paper.similar import (
    GROUP_COMPETITOR,
    GROUP_FOUNDATION,
    SimilarFinder,
    _build_query,
    _key,
)


class TestParseRef:
    def test_arxiv_forms(self) -> None:
        for text in (
            "https://arxiv.org/abs/1706.03762",
            "https://arxiv.org/abs/1706.03762v5",
            "https://arxiv.org/pdf/1706.03762",
            "arXiv:1706.03762",
            "1706.03762",
        ):
            assert parse_ref(text).arxiv_id == "1706.03762", text

    def test_arxiv_doi_reduced_to_id(self) -> None:
        """arXiv DOI 必须还原为 ID——实测该 DOI 在 OpenAlex 查不到，
        但 arXiv API 用 ID 可以。"""
        ref = parse_ref("10.48550/arXiv.2005.11401")
        assert ref.arxiv_id == "2005.11401"

    def test_plain_doi(self) -> None:
        assert parse_ref("10.1145/3576915").doi == "10.1145/3576915"
        assert parse_ref("https://doi.org/10.1145/3576915").doi == "10.1145/3576915"

    def test_doi_trailing_punctuation_stripped(self) -> None:
        assert parse_ref("见 10.1145/3576915.").doi == "10.1145/3576915"

    def test_local_pdf(self) -> None:
        ref = parse_ref("/tmp/2301.00234.pdf")
        assert ref.pdf_path is not None
        # 文件名里的 arXiv ID 应被识别，便于补元数据
        assert ref.arxiv_id == "2301.00234"

    def test_title_fallback(self) -> None:
        ref = parse_ref("Attention Is All You Need")
        assert ref.title == "Attention Is All You Need"
        assert ref.arxiv_id is None

    def test_empty(self) -> None:
        assert parse_ref("").is_empty

    def test_cache_key_stable_across_forms(self) -> None:
        """同一篇论文的不同输入形式必须命中同一缓存，否则重复请求。"""
        a = parse_ref("https://arxiv.org/abs/1706.03762")
        b = parse_ref("arXiv:1706.03762v3")
        assert a.cache_key == b.cache_key

    def test_s2_lookup_id_priority(self) -> None:
        ref = parse_ref("https://arxiv.org/abs/1706.03762")
        assert ref.s2_lookup_id == "arXiv:1706.03762"


class TestSectionDetection:
    def test_common_sections(self) -> None:
        assert _detect_section("Abstract") == "abstract"
        assert _detect_section("1 Introduction") == "introduction"
        assert _detect_section("## 3 Method") == "method"
        assert _detect_section("**4. Experiments**") == "experiment"
        assert _detect_section("5 Conclusion") == "conclusion"
        assert _detect_section("References") == "references"

    def test_body_text_not_mistaken_for_heading(self) -> None:
        long_line = "We propose a new method that " + "x" * 100
        assert _detect_section(long_line) is None

    def test_references_with_trailing_content(self) -> None:
        """回归测试：References 标题曾因带后缀而漏识别，
        导致几十条参考文献混入正文章节，白烧大量 token。"""
        assert _detect_section("References  ") == "references"
        assert _detect_section("## References") == "references"


class TestReferenceEntryDetection:
    def test_numbered_entries(self) -> None:
        assert _looks_like_reference(
            "- [26] David McClosky, Eugene Charniak. Effective self-training. 2006."
        )
        assert _looks_like_reference(
            "[15] Ashish Vaswani et al. In Proceedings of NeurIPS."
        )

    def test_body_text_not_flagged(self) -> None:
        assert not _looks_like_reference(
            "We propose the Transformer, a model based on attention."
        )

    def test_needs_year_or_venue(self) -> None:
        # 编号开头但无年份/出版信息 → 不判定为参考文献
        assert not _looks_like_reference("[1] see appendix for details")


class TestSplitSections:
    def test_references_excluded_from_body(self) -> None:
        text = """# Abstract
This paper proposes a new approach for sequence modeling with strong results.

## 1 Introduction
Recurrent models process tokens sequentially which limits parallelization badly.

## References
- [1] Alice Smith. Some earlier work. In Proceedings of ACL, 2018.
- [2] Bob Jones. Another prior study. In Proceedings of EMNLP, 2019.
"""
        result = _split_sections(text)
        sections = {s.section for s in result.segments}
        assert "references" not in sections
        assert "abstract" in sections

    def test_unlabeled_references_caught_by_fallback(self) -> None:
        """References 标题缺失时，靠内容特征兜底剔除。"""
        text = """## 5 Conclusion
We presented the Transformer model and showed it achieves strong results overall.

- [1] Alice Smith. Some earlier work here. In Proceedings of ACL, 2018.
- [2] Bob Jones. Another prior study here. In Proceedings of EMNLP, 2019.
- [3] Carol White. A third related paper. In Proceedings of NAACL, 2020.
"""
        result = _split_sections(text)
        bodies = [s.text for s in result.segments]
        assert any("Transformer" in b for b in bodies)
        assert not any("[2] Bob Jones" in b for b in bodies)

    def test_priority_sections_configured(self) -> None:
        # 高价值章节必须包含方法与实验；参考文献必须在跳过列表
        assert "method" in PRIORITY_SECTIONS
        assert "experiment" in PRIORITY_SECTIONS
        assert "references" in SKIP_SECTIONS


class TestCredibility:
    def test_unknown_status_not_reported_as_preprint(self) -> None:
        """核心修复：数据源限流时不能误报「预印本 / 未经同行评审」。"""
        meta = PaperMeta(title="x", is_preprint=None)
        assert meta.publication_status == "发表状态未知"
        assert meta.credibility["peer_reviewed"] is None

    def test_published_with_venue(self) -> None:
        meta = PaperMeta(title="x", venue="NeurIPS 2017", is_preprint=False)
        assert meta.credibility["peer_reviewed"] is True
        assert meta.publication_status == "已正式发表"

    def test_explicit_preprint(self) -> None:
        meta = PaperMeta(title="x", is_preprint=True)
        assert meta.publication_status == "预印本"
        assert meta.credibility["peer_reviewed"] is False


class TestSimilarHelpers:
    def test_dedupe_key_prefers_stable_id(self) -> None:
        a = CitationRef(title="X", arxiv_id="1706.03762", s2_id="abc")
        b = CitationRef(title="X different case", arxiv_id="1706.03762")
        assert _key(a) == _key(b)

    def test_query_drops_stopwords(self) -> None:
        q = _build_query(PaperMeta(title="Attention Is All You Need"))
        assert "Attention" in q
        # you / need / all 是停用词，检索时无价值
        assert "You" not in q.split()

    def test_query_keeps_technical_terms(self) -> None:
        q = _build_query(
            PaperMeta(title="Retrieval-Augmented Generation for Knowledge-Intensive NLP")
        )
        assert "Retrieval-Augmented" in q
        assert "NLP" in q


class StubClient:
    """离线 stub：可控地模拟各路召回的成功与失败。"""

    def __init__(self, references=None, citations=None, search=None) -> None:
        self._refs = references or []
        self._cites = citations or []
        self._search = search or []
        self.degraded: list[str] = []
        self.citation_calls = 0

    def fetch_references(self, ref, *, limit=60):
        return list(self._refs)

    def fetch_citations(self, ref, *, limit=60):
        self.citation_calls += 1
        return list(self._cites)

    def search_s2(self, query, *, limit=10):
        return list(self._search)


class TestSimilarFinder:
    def _meta(self) -> PaperMeta:
        return PaperMeta(title="Target Paper On Retrieval", year=2020, arxiv_id="2005.11401")

    def test_foundation_sorted_by_citations(self) -> None:
        refs = [
            CitationRef(title="Low impact work", arxiv_id="a1", citation_count=10),
            CitationRef(title="Seminal work", arxiv_id="a2", citation_count=50_000),
            CitationRef(title="Mid work", arxiv_id="a3", citation_count=800),
        ]
        finder = SimilarFinder(StubClient(references=refs))
        result = finder.find(parse_ref("arXiv:2005.11401"), self._meta(), limit_per_group=3)
        titles = [p.title for p in result.groups[GROUP_FOUNDATION]]
        assert titles[0] == "Seminal work"

    def test_generic_papers_excluded_from_coupling(self) -> None:
        """回归测试：共同引用 Attention/Adam 这类超高被引论文说明不了相似性，
        实测曾因此召回物理、跨语言对齐等完全不相关的工作。"""
        refs = [
            CitationRef(title="Attention is All you Need", arxiv_id="g1", citation_count=192_659),
            CitationRef(title="Adam Optimizer", arxiv_id="g2", citation_count=171_034),
        ]
        client = StubClient(references=refs, citations=[])
        finder = SimilarFinder(client)
        result = finder.find(parse_ref("arXiv:2005.11401"), self._meta())
        # 全是通用论文 → 该路应跳过而非乱召回
        assert not result.groups.get(GROUP_COMPETITOR)
        assert any("通用基础论文" in n for n in result.notes + result.degraded)

    def test_domain_specific_refs_used_for_coupling(self) -> None:
        refs = [
            CitationRef(title="Domain specific method", arxiv_id="d1",
                        citation_count=800, year=2019),
        ]
        cites = [
            CitationRef(title="Competitor A", arxiv_id="c1", citation_count=30),
        ]
        client = StubClient(references=refs, citations=cites)
        finder = SimilarFinder(client)
        finder.find(parse_ref("arXiv:2005.11401"), self._meta())
        assert client.citation_calls >= 1

    def test_degradation_reported_not_silent(self) -> None:
        """任一路失败必须明确告知，不能静默返回残缺结果。"""
        finder = SimilarFinder(StubClient())
        result = finder.find(parse_ref("arXiv:2005.11401"), self._meta())
        assert result.degraded
        assert any("参考文献" in d for d in result.degraded)

    def test_unread_filter(self) -> None:
        class FakeDB:
            def find_source_by_url(self, url):
                class S:
                    id = 7
                return S() if "a2" in url else None

        refs = [
            CitationRef(title="In library", arxiv_id="a2", citation_count=5000),
            CitationRef(title="Not in library", arxiv_id="a9", citation_count=4000),
        ]
        finder = SimilarFinder(StubClient(references=refs), db=FakeDB())
        result = finder.find(
            parse_ref("arXiv:2005.11401"), self._meta(), unread_only=True
        )
        titles = [p.title for p in result.flat()]
        assert "In library" not in titles
        assert "Not in library" in titles


class TestLineage:
    def test_orders_foundational_first(self) -> None:
        refs = [
            CitationRef(title="Very foundational", arxiv_id="f1",
                        citation_count=100_000, year=2014),
            CitationRef(title="Recent minor", arxiv_id="f2",
                        citation_count=150, year=2019),
        ]
        lineage = build_lineage(
            StubClient(references=refs),
            parse_ref("arXiv:2005.11401"),
            PaperMeta(title="Target", year=2020),
        )
        assert lineage.steps
        assert lineage.steps[0].title == "Very foundational"

    def test_low_citation_refs_skipped(self) -> None:
        """低被引参考文献多为细节引用，不是必读前置。"""
        refs = [CitationRef(title="Minor detail", arxiv_id="m1", citation_count=5)]
        lineage = build_lineage(
            StubClient(references=refs),
            parse_ref("arXiv:2005.11401"),
            PaperMeta(title="Target", year=2020),
        )
        assert not lineage.steps
        assert lineage.notes

    def test_no_references_degrades(self) -> None:
        lineage = build_lineage(
            StubClient(), parse_ref("arXiv:2005.11401"), PaperMeta(title="T")
        )
        assert lineage.degraded


class TestApiCache:
    def test_roundtrip_and_expiry(self, tmp_path: Path) -> None:
        from distiller.paper.cache import ApiCache

        cache = ApiCache(tmp_path / "c.db", ttl_days=30)
        assert cache.get("k") is None
        cache.set("k", {"v": 1})
        assert cache.get("k") == {"v": 1}
        assert cache.hits == 1

        expired = ApiCache(tmp_path / "c.db", ttl_days=0)
        assert expired.get("k") is None  # TTL 为 0 → 立即过期
        cache.close()
        expired.close()
