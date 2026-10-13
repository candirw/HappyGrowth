"""论文标识解析：任意输入 → 规范化标识。

支持：arXiv 链接/ID、DOI、S2 paperId、OpenAlex ID、本地 PDF、纯标题。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_ARXIV_PATTERNS = (
    re.compile(r"arxiv\.org/abs/([0-9]{4}\.[0-9]{4,5})(v\d+)?", re.I),
    re.compile(r"arxiv\.org/pdf/([0-9]{4}\.[0-9]{4,5})(v\d+)?", re.I),
    re.compile(r"\barxiv[:\s]+([0-9]{4}\.[0-9]{4,5})(v\d+)?", re.I),
    re.compile(r"^([0-9]{4}\.[0-9]{4,5})(v\d+)?$"),
    # 旧式 ID，如 cs/0501001
    re.compile(r"arxiv\.org/abs/([a-z\-]+(?:\.[A-Z]{2})?/[0-9]{7})", re.I),
)

_DOI_PATTERN = re.compile(r"\b(10\.\d{4,9}/[-._;()/:a-z0-9]+)", re.I)
_S2_PATTERN = re.compile(r"^[0-9a-f]{40}$", re.I)
_OPENALEX_PATTERN = re.compile(r"\b(W\d{6,12})\b")


@dataclass
class PaperRef:
    """论文引用标识。至少有一项非空。"""

    arxiv_id: str | None = None
    doi: str | None = None
    s2_id: str | None = None
    openalex_id: str | None = None
    title: str | None = None
    pdf_path: Path | None = None
    raw: str = ""

    @property
    def is_empty(self) -> bool:
        return not any(
            (self.arxiv_id, self.doi, self.s2_id, self.openalex_id, self.title, self.pdf_path)
        )

    @property
    def s2_lookup_id(self) -> str | None:
        """S2 API 接受的标识形式，按可靠性排序。"""
        if self.arxiv_id:
            return f"arXiv:{self.arxiv_id}"
        if self.doi:
            return f"DOI:{self.doi}"
        if self.s2_id:
            return self.s2_id
        return None

    @property
    def cache_key(self) -> str:
        """缓存键。同一篇论文的不同输入形式应命中同一缓存。"""
        for prefix, value in (
            ("arxiv", self.arxiv_id),
            ("doi", self.doi),
            ("s2", self.s2_id),
            ("oa", self.openalex_id),
        ):
            if value:
                return f"{prefix}:{value.lower()}"
        if self.title:
            import hashlib

            digest = hashlib.sha1(self.title.lower().encode("utf-8")).hexdigest()[:16]
            return f"title:{digest}"
        if self.pdf_path:
            return f"pdf:{self.pdf_path.stem[:40]}"
        return "unknown"

    def describe(self) -> str:
        if self.arxiv_id:
            return f"arXiv:{self.arxiv_id}"
        if self.doi:
            return f"DOI:{self.doi}"
        if self.pdf_path:
            return f"本地 PDF：{self.pdf_path.name}"
        if self.title:
            return f"标题：{self.title[:50]}"
        return self.raw[:50]


def parse_ref(text: str) -> PaperRef:
    """解析论文标识。识别不出结构化 ID 时按标题处理。"""
    text = (text or "").strip()
    ref = PaperRef(raw=text)
    if not text:
        return ref

    # 本地文件优先（避免把路径里的数字误认为 ID）
    candidate = Path(text).expanduser()
    if candidate.suffix.lower() == ".pdf":
        ref.pdf_path = candidate
        # 文件名可能就是 arXiv ID
        if m := re.match(r"^([0-9]{4}\.[0-9]{4,5})", candidate.stem):
            ref.arxiv_id = m.group(1)
        return ref

    for pattern in _ARXIV_PATTERNS:
        if m := pattern.search(text):
            ref.arxiv_id = m.group(1)
            return ref

    # arXiv DOI（10.48550/arXiv.xxxx）应还原为 arXiv ID：
    # 实测 OpenAlex 用该 DOI 查不到，但 arXiv API 用 ID 可以
    if m := re.search(r"10\.48550/arxiv\.([0-9]{4}\.[0-9]{4,5})", text, re.I):
        ref.arxiv_id = m.group(1)
        ref.doi = f"10.48550/arXiv.{m.group(1)}"
        return ref

    if m := _DOI_PATTERN.search(text):
        ref.doi = m.group(1).rstrip(".,;)")
        return ref

    if _S2_PATTERN.match(text):
        ref.s2_id = text.lower()
        return ref

    if m := _OPENALEX_PATTERN.search(text):
        ref.openalex_id = m.group(1)
        return ref

    # 兜底：当作标题
    if len(text) >= 8 and not text.startswith("http"):
        ref.title = text
    return ref
