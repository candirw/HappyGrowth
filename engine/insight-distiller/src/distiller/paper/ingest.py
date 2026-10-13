"""论文采集：标识 → 元数据 → PDF → 分节片段。

放在 paper 模块而非 ingest：论文采集需要 API 缓存与数据目录，
与普通网页采集器的无状态接口不匹配。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import httpx

from ..compat.paths import DataPaths, safe_filename
from ..models import ContentType, RawContent, Segment
from .clients import AcademicClient, PaperMeta
from .parse import PaperParseError, parse_pdf
from .resolve import PaperRef, parse_ref

ProgressFn = Callable[[str], None]


def is_paper_url(text: str) -> bool:
    """判断输入是否为论文标识。用于 pipeline 分流。"""
    lowered = (text or "").strip().lower()
    return (
        "arxiv.org/abs/" in lowered
        or "arxiv.org/pdf/" in lowered
        or lowered.startswith("arxiv:")
        or "doi.org/" in lowered
        or lowered.startswith("10.")
        or lowered.endswith(".pdf")
    )


def download_pdf(
    url: str,
    out_dir: Path,
    *,
    basename: str,
    progress: ProgressFn | None = None,
) -> Path | None:
    """下载 PDF。已存在则复用（缓存）。失败返回 None 而非抛异常。"""
    progress = progress or (lambda _m: None)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{safe_filename(basename)}.pdf"

    if target.exists() and target.stat().st_size > 10_240:
        progress(f"复用已下载的 PDF：{target.name}")
        return target

    try:
        progress("下载 PDF")
        with httpx.stream(
            "GET", url, timeout=90, follow_redirects=True,
            headers={"User-Agent": "insight-distiller/0.1"},
        ) as resp:
            if resp.status_code != 200:
                progress(f"PDF 下载失败：HTTP {resp.status_code}")
                return None
            ctype = resp.headers.get("content-type", "")
            if "pdf" not in ctype.lower() and "octet-stream" not in ctype.lower():
                progress(f"返回内容不是 PDF（{ctype}）")
                return None
            # 用 Path.open 而非内置 open：便于静态检查区分
            # 「二进制写入」与「需指定 encoding 的文本读写」
            with target.open("wb") as f:
                for chunk in resp.iter_bytes(chunk_size=65536):
                    f.write(chunk)
    except httpx.RequestError as exc:
        progress(f"PDF 下载失败：{exc}")
        return None

    if target.exists() and target.stat().st_size > 10_240:
        progress(f"已下载 {target.stat().st_size // 1024} KB")
        return target
    target.unlink(missing_ok=True)
    return None


def ingest_paper(
    text: str,
    *,
    client: AcademicClient,
    paths: DataPaths,
    progress: ProgressFn | None = None,
    fulltext: bool = True,
) -> tuple[RawContent, PaperRef, PaperMeta]:
    """采集一篇论文。

    降级链：全文 PDF → 仅摘要 → 明确失败。
    只有摘要时仍能产出有价值的浓缩，因此不轻易失败。
    """
    progress = progress or (lambda _m: None)
    ref = parse_ref(text)
    if ref.is_empty:
        raise PaperParseError(
            f"无法识别论文标识：{text[:60]}\n"
            "支持：arXiv 链接/ID、DOI、本地 PDF 路径、论文标题"
        )

    progress(f"识别为 {ref.describe()}")
    meta = client.resolve_meta(ref)

    warnings = list(meta.warnings)
    segments: list[Segment] = []
    pdf_path: Path | None = None

    # 本地 PDF 优先
    if ref.pdf_path and ref.pdf_path.exists():
        pdf_path = ref.pdf_path
    elif fulltext and meta.pdf_url:
        pdf_path = download_pdf(
            meta.pdf_url,
            paths.papers,
            basename=meta.arxiv_id or (meta.title or "paper")[:60],
            progress=progress,
        )

    if pdf_path:
        try:
            progress("解析 PDF 分节")
            parsed = parse_pdf(pdf_path)
            segments = parsed.segments
            warnings.extend(parsed.warnings)
            if parsed.sections_found:
                progress(
                    f"识别章节：{'、'.join(parsed.sections_found)}"
                    f"（{len(segments)} 段 / {parsed.char_count} 字）"
                )
        except PaperParseError as exc:
            warnings.append(f"PDF 解析失败，降级为仅摘要：{exc}")

    # 降级：只用摘要
    if not segments:
        if not meta.abstract:
            raise PaperParseError(
                "既无法获取全文 PDF，也没有摘要，无法浓缩。\n"
                "可下载 PDF 后用 `distill add /path/to/paper.pdf` 导入。"
                + (f"\n数据源状态：{'; '.join(client.degraded)}" if client.degraded else "")
            )
        segments = [Segment(idx=0, text=meta.abstract, section="abstract")]
        warnings.append("仅使用摘要浓缩（未取到全文），结论覆盖面有限")

    url = _canonical_url(ref, meta)
    raw = RawContent(
        url=url,
        content_type=ContentType.PAPER,
        platform="arxiv" if meta.arxiv_id else "paper",
        title=meta.title or ref.describe(),
        author="; ".join(meta.authors[:8]) if meta.authors else None,
        lang="en",
        segments=segments,
        warnings=warnings,
        extra={
            "arxiv_id": meta.arxiv_id,
            "doi": meta.doi,
            "s2_id": meta.s2_id,
            "year": meta.year,
            "venue": meta.venue,
            "citation_count": meta.citation_count,
            "is_preprint": meta.is_preprint,
            "categories": meta.categories,
            "sources_used": meta.sources_used,
            "pdf_rel_path": paths.relative(pdf_path) if pdf_path else None,
        },
    )
    return raw, ref, meta


def _canonical_url(ref: PaperRef, meta: PaperMeta) -> str:
    """规范化 URL 作为唯一键。

    同一篇论文的不同入口（abs 页、pdf 页、DOI）必须映射到同一 URL，
    否则会重复消化、重复花钱。
    """
    arxiv_id = meta.arxiv_id or ref.arxiv_id
    if arxiv_id:
        return f"https://arxiv.org/abs/{arxiv_id}"
    doi = meta.doi or ref.doi
    if doi:
        return f"https://doi.org/{doi}"
    if meta.s2_id:
        return f"https://www.semanticscholar.org/paper/{meta.s2_id}"
    if ref.pdf_path:
        return f"file://{ref.pdf_path.name}"
    return f"paper://{ref.cache_key}"
