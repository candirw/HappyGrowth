"""论文模块（M3）。

已按实测修订选型（见 DECISIONS.md）：
- arXiv 为元数据主力（无限流，最稳定）
- Semantic Scholar 为引文图谱主力（需退避重试）
- OpenAlex 仅单篇查询作补充（list 类请求已需付费）
"""

from .cache import ApiCache
from .clients import AcademicClient, CitationRef, PaperMeta
from .lineage import Lineage, ReadingStep, build_lineage, compare_papers
from .parse import ParsedPaper, PaperParseError, parse_pdf
from .resolve import PaperRef, parse_ref
from .similar import (
    GROUP_COMPETITOR,
    GROUP_CROSSOVER,
    GROUP_FOLLOWUP,
    GROUP_FOUNDATION,
    SimilarFinder,
    SimilarPaper,
    SimilarResult,
)
from .summarize import PaperSummarizer

__all__ = [
    "AcademicClient",
    "ApiCache",
    "CitationRef",
    "GROUP_COMPETITOR",
    "GROUP_CROSSOVER",
    "GROUP_FOLLOWUP",
    "GROUP_FOUNDATION",
    "Lineage",
    "PaperMeta",
    "PaperParseError",
    "PaperRef",
    "PaperSummarizer",
    "ParsedPaper",
    "ReadingStep",
    "SimilarFinder",
    "SimilarPaper",
    "SimilarResult",
    "build_lineage",
    "compare_papers",
    "parse_pdf",
    "parse_ref",
]
