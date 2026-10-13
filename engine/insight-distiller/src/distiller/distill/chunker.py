"""语义分块。

要点：
- 按 segment 边界切分，绝不切断句子，保证每个 chunk 可回溯到原文片段。
- 中文按字符估算 token（约 1.5 字/token），英文按词，避免引入 tiktoken 依赖。
- 保留重叠，防止跨块论述被割裂。
"""

from __future__ import annotations

from ..models import Chunk, Segment


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数。

    中文约 1.5 字符/token，英文约 4 字符/token。
    不引入 tiktoken：它对非 OpenAI 模型本就不准，且是额外的跨平台依赖。
    """
    han = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - han
    return int(han / 1.5 + other / 4) + 1


def chunk_segments(
    segments: list[Segment],
    *,
    chunk_size: int = 2000,
    overlap: int = 200,
) -> list[Chunk]:
    """把片段合并成不超过 chunk_size token 的语义块。"""
    if not segments:
        return []

    chunks: list[Chunk] = []
    buf: list[Segment] = []
    buf_tokens = 0

    def flush() -> None:
        nonlocal buf, buf_tokens
        if not buf:
            return
        chunks.append(
            Chunk(
                idx=len(chunks),
                text="\n".join(s.text for s in buf),
                segment_idxs=[s.idx for s in buf],
            )
        )
        # 保留尾部若干片段作为重叠，维持上下文连续性
        if overlap > 0:
            tail: list[Segment] = []
            tail_tokens = 0
            for seg in reversed(buf):
                seg_tokens = estimate_tokens(seg.text)
                if tail_tokens + seg_tokens > overlap:
                    break
                tail.insert(0, seg)
                tail_tokens += seg_tokens
            buf = tail
            buf_tokens = tail_tokens
        else:
            buf = []
            buf_tokens = 0

    for seg in segments:
        seg_tokens = estimate_tokens(seg.text)

        # 单个片段就超限：独立成块，不切断原文
        if seg_tokens >= chunk_size:
            flush()
            if buf:
                flush()
            chunks.append(
                Chunk(idx=len(chunks), text=seg.text, segment_idxs=[seg.idx])
            )
            buf, buf_tokens = [], 0
            continue

        if buf_tokens + seg_tokens > chunk_size:
            flush()

        buf.append(seg)
        buf_tokens += seg_tokens

    # 最后一块：清空重叠逻辑直接落盘
    if buf:
        chunks.append(
            Chunk(
                idx=len(chunks),
                text="\n".join(s.text for s in buf),
                segment_idxs=[s.idx for s in buf],
            )
        )

    # 去掉 overlap 机制可能产生的完全重复块。
    # 注意：必须用 segment_idxs 判断，不能用文本前缀——
    # 前缀相同但内容不同的块（重复开场白、结构化列表）会被误删，
    # 导致原文大量静默丢失。
    deduped: list[Chunk] = []
    seen: set[tuple[int, ...]] = set()
    for c in chunks:
        key = tuple(c.segment_idxs)
        if key and key in seen:
            continue
        seen.add(key)
        deduped.append(
            Chunk(idx=len(deduped), text=c.text, segment_idxs=c.segment_idxs)
        )
    return deduped
