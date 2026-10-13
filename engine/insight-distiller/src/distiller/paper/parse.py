"""论文 PDF 分节抽取。

**分节的价值**：浓缩时可只把 Method + Experiment 送给 LLM，
token 省一半且结论更准（Related Work 与致谢对理解方法几乎无贡献）。

三个实际难点的处理：
- 双栏排版乱序 → pymupdf4llm 已处理
- 公式 → 保留原文或占位，不强求识别（成本高、收益低）
- 图表 → 只抽 caption
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..models import Segment

# 章节标题识别。论文结构相对固定，规则足够可靠。
#
# 注意：一律用前缀匹配而非 \b 词边界结尾。
# 用 \b 会导致复数形式漏识别（"Experiments" 匹配不上 r"experiment\b"），
# 而论文里 "4. Experiments"、"Results and Discussion" 这类写法极常见。
# 编号形式覆盖 "4"、"4."、"4.1"、"IV" 等。
_NUM_PREFIX = r"(?:\d+(?:\.\d+)*\.?\s+|[IVX]+\.?\s+)?"

_SECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("abstract", re.compile(rf"^{_NUM_PREFIX}abstract", re.I)),
    ("introduction", re.compile(rf"^{_NUM_PREFIX}introduction", re.I)),
    ("related_work", re.compile(rf"^{_NUM_PREFIX}related\s+work", re.I)),
    ("background", re.compile(rf"^{_NUM_PREFIX}(?:background|preliminar)", re.I)),
    ("method", re.compile(
        rf"^{_NUM_PREFIX}(?:method|methodolog|approach|model\s+architecture|"
        rf"our\s+(?:method|approach|model)|proposed\s+|the\s+model\b)", re.I)),
    ("experiment", re.compile(
        rf"^{_NUM_PREFIX}(?:experiment|experimental|evaluation|result|"
        rf"empirical)", re.I)),
    ("ablation", re.compile(rf"^{_NUM_PREFIX}ablation", re.I)),
    ("discussion", re.compile(rf"^{_NUM_PREFIX}discussion", re.I)),
    ("limitation", re.compile(
        rf"^{_NUM_PREFIX}(?:limitation|threats\s+to\s+validity)", re.I)),
    ("conclusion", re.compile(
        rf"^{_NUM_PREFIX}(?:conclusion|concluding|summary\s*$)", re.I)),
    ("references", re.compile(rf"^{_NUM_PREFIX}(?:reference|bibliograph)", re.I)),
    ("appendix", re.compile(rf"^{_NUM_PREFIX}appendi", re.I)),
    ("acknowledgment", re.compile(rf"^{_NUM_PREFIX}acknowledg", re.I)),
]

# 浓缩时优先送入 LLM 的章节（按信息密度排序）
PRIORITY_SECTIONS = (
    "abstract", "introduction", "method", "experiment",
    "ablation", "limitation", "conclusion", "discussion",
)
# 明确排除：对理解方法贡献极低，但占大量 token
SKIP_SECTIONS = ("references", "acknowledgment", "appendix")


class PaperParseError(RuntimeError):
    pass


@dataclass
class ParsedPaper:
    segments: list[Segment] = field(default_factory=list)
    sections_found: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    char_count: int = 0

    def priority_segments(self) -> list[Segment]:
        """只取高价值章节。缺少分节信息时返回全部。"""
        if not self.sections_found:
            return self.segments
        picked = [
            s for s in self.segments
            if s.section and s.section.split(":")[0] in PRIORITY_SECTIONS
        ]
        return picked or self.segments


def parse_pdf(path: Path, *, max_pages: int | None = None) -> ParsedPaper:
    """解析 PDF 为分节片段。"""
    path = Path(path)
    if not path.exists():
        raise PaperParseError(f"文件不存在：{path}")

    text, warnings = _extract_text(path, max_pages=max_pages)
    if not text.strip():
        raise PaperParseError(
            "未能从 PDF 提取到文本。可能是扫描版（需 OCR）或加密文件。"
        )

    result = _split_sections(text)
    result.warnings.extend(warnings)
    result.char_count = sum(len(s.text) for s in result.segments)

    if not result.sections_found:
        result.warnings.append(
            "未能识别论文章节结构（可能是非标准排版），将按整体处理"
        )
    else:
        missing = [
            s for s in ("abstract", "method", "experiment")
            if s not in result.sections_found
        ]
        if missing:
            result.warnings.append(
                f"未识别到章节：{'、'.join(missing)}，浓缩结果可能不完整"
            )
    return result


def _extract_text(path: Path, *, max_pages: int | None = None) -> tuple[str, list[str]]:
    """抽取文本。pymupdf4llm 优先（保留结构），失败降级纯文本。"""
    warnings: list[str] = []

    try:
        import pymupdf4llm

        kwargs = {}
        if max_pages:
            kwargs["pages"] = list(range(max_pages))
        text = pymupdf4llm.to_markdown(str(path), **kwargs)
        if text and text.strip():
            return text, warnings
        warnings.append("pymupdf4llm 未提取到内容，已降级为纯文本抽取")
    except ImportError:
        warnings.append(
            '未安装 pymupdf4llm，使用基础文本抽取：pip install -e ".[paper]"'
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"pymupdf4llm 解析失败（{exc}），已降级")

    try:
        import pymupdf

        doc = pymupdf.open(str(path))
        pages = doc[: max_pages] if max_pages else doc
        chunks = [page.get_text() for page in pages]
        doc.close()
        return "\n".join(chunks), warnings
    except ImportError as exc:
        raise PaperParseError(
            '未安装 PDF 解析依赖。请执行：pip install -e ".[paper]"'
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise PaperParseError(f"PDF 解析失败：{exc}") from exc


def _detect_section(line: str) -> str | None:
    """判断该行是否为章节标题。"""
    stripped = line.strip()
    # 去掉 markdown 标题符号与粗体标记
    stripped = re.sub(r"^#+\s*", "", stripped)
    stripped = stripped.strip("*_# ").strip()
    # 章节标题通常很短；过长的是正文
    if not stripped or len(stripped) > 80:
        return None
    for name, pattern in _SECTION_PATTERNS:
        if pattern.match(stripped):
            return name
    return None


# 参考文献条目特征：编号开头 + 含年份或出版信息。
# 需要这个兜底是因为 References 标题常因排版差异漏识别，
# 一旦漏识别，几十条参考文献会被并进上一章节，白烧大量 token。
_REF_ENTRY_RE = re.compile(
    r"^\s*(?:[-*]\s*)?\[\d{1,3}\]\s+\S"            # [12] Author...
    r"|^\s*(?:[-*]\s*)?\d{1,3}\.\s+[A-Z][a-z]+,\s"  # 12. Smith, J...
)


def _looks_like_reference(text: str) -> bool:
    if not _REF_ENTRY_RE.match(text):
        return False
    # 参考文献几乎必然含年份或出版标识
    return bool(
        re.search(r"\b(19|20)\d{2}\b", text)
        or re.search(r"arxiv|doi:|in proceedings|preprint|journal", text, re.I)
    )


def _split_sections(text: str) -> ParsedPaper:
    """按章节切分并生成片段。"""
    result = ParsedPaper()
    current = "header"
    buffer: list[str] = []
    section_order: list[str] = []

    def flush() -> None:
        nonlocal current
        if not buffer:
            return
        body = "\n".join(buffer).strip()
        if not body:
            buffer.clear()
            return
        # 参考文献等章节不入正文，但记录其存在
        if current in SKIP_SECTIONS:
            buffer.clear()
            return

        paragraphs = _paragraphs(body)
        # 兜底：连续出现参考文献条目 → 判定后续均为参考文献。
        # References 标题常因排版漏识别，不兜底会让几十条参考文献
        # 混入正文章节，浪费大量 token 且污染摘要。
        ref_streak = 0
        for para in paragraphs:
            if _looks_like_reference(para):
                ref_streak += 1
                if ref_streak >= 2:
                    current = "references"
                    buffer.clear()
                    return
                continue
            ref_streak = 0
            result.segments.append(
                Segment(idx=len(result.segments), text=para, section=current)
            )
        buffer.clear()

    for line in text.splitlines():
        detected = _detect_section(line)
        if detected:
            flush()
            current = detected
            if detected not in section_order:
                section_order.append(detected)
            continue
        buffer.append(line)

    flush()
    result.sections_found = [s for s in section_order if s not in SKIP_SECTIONS]
    return result


_FORMULA_RE = re.compile(r"\$\$.*?\$\$", re.S)
_FIG_RE = re.compile(r"^!\[.*?\]\(.*?\)\s*$", re.M)


def _paragraphs(text: str, *, min_len: int = 40) -> list[str]:
    """切段并清理。

    公式转占位符：保留其存在信息但不占 token；
    图片链接直接删除，caption 会作为普通文本保留。
    """
    text = _FORMULA_RE.sub(" [FORMULA] ", text)
    text = _FIG_RE.sub("", text)

    paragraphs: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        cleaned = re.sub(r"\s+", " ", block).strip()
        # 过滤页眉页脚、页码、分隔线
        if len(cleaned) < min_len:
            continue
        if re.fullmatch(r"[\d\s\-—_=.·|]+", cleaned):
            continue
        paragraphs.append(cleaned)
    return paragraphs
