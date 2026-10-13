"""混合检索：向量 + BM25 → RRF 融合。

**为什么必须混合**：纯向量检索在专有名词、数字、代码标识符上召回很差
（问"3.2% 提升"时向量可能完全找不到）；纯 BM25 又无法处理同义表达。
两路召回 + RRF 融合是工程上性价比最高的方案。

RRF（Reciprocal Rank Fusion）而非分数加权：两路的分数量纲不可比
（余弦相似度 vs BM25 分数），按排名融合更稳健且无需调参。
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Callable

from ..config import RetrievalConfig
from ..store import Database
from .embedder import Embedder, EmbeddingError

ProgressFn = Callable[[str], None]


@dataclass
class Hit:
    chunk_id: int
    source_id: int
    text: str
    title: str
    url: str
    author: str | None
    segment_idxs: list[int]
    score: float
    anchor: str | None = None
    vector_rank: int | None = None
    keyword_rank: int | None = None

    @property
    def cite(self) -> str:
        """引用标记，如 [S3 12:34]，让回答可回溯到原文位置。"""
        return f"S{self.source_id}" + (f" {self.anchor}" if self.anchor else "")


def _tokenize_for_fts(query: str) -> str:
    """把自然语言问题转成 FTS5 可用的查询。

    实现在 store.fts 里，与**索引侧共用**——两边各写一份正是
    中文 BM25 曾经完全失效的原因（查询侧切了二元组、索引侧没切）。
    这里保留薄封装，因为已有测试与调用方按这个名字引用。
    """
    from ..store.fts import query_text

    return query_text(query)


class Retriever:
    def __init__(
        self,
        db: Database,
        config: RetrievalConfig,
        *,
        progress: ProgressFn | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.progress = progress or (lambda _m: None)
        self._embedder: Embedder | None = None

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = Embedder(
                self.config.embedding_model, progress=self.progress
            )
        return self._embedder

    # ---------- 索引 ----------

    def index_source(self, source_id: int, segments: list) -> int:
        """为一篇内容建立检索索引。返回索引的 chunk 数。"""
        from ..distill.chunker import chunk_segments

        if not segments:
            return 0

        chunks = chunk_segments(
            segments,
            chunk_size=self.config.chunk_size,
            overlap=self.config.chunk_overlap,
        )
        chunk_ids = self.db.replace_chunks(source_id, chunks)

        vectors = self.embedder.encode([c.text for c in chunks])
        self.db.save_embeddings(
            source_id=source_id,
            chunk_ids=chunk_ids,
            vectors=vectors,
            model=self.config.embedding_model,
        )
        return len(chunks)

    # ---------- 检索 ----------

    def search(
        self,
        query: str,
        *,
        source_ids: list[int] | None = None,
        top_n: int | None = None,
    ) -> list[Hit]:
        top_n = top_n or self.config.top_n
        vector_hits = self._vector_search(query, source_ids)
        keyword_hits = (
            self._keyword_search(query, source_ids) if self.config.hybrid else []
        )

        fused = self._rrf_fuse(vector_hits, keyword_hits)
        top = fused[:top_n]
        self._attach_anchors(top)
        return top

    def _vector_search(
        self, query: str, source_ids: list[int] | None
    ) -> list[tuple[int, float]]:
        try:
            qvec = self.embedder.encode_one(query, is_query=True)
        except EmbeddingError as exc:
            self.progress(f"向量检索不可用：{exc}")
            return []

        chunk_ids, blobs, dim = self.db.load_all_embeddings(
            self.config.embedding_model, source_ids=source_ids
        )
        if not chunk_ids:
            return []

        try:
            import numpy as np
        except ImportError:
            self.progress("未安装 numpy，跳过向量检索")
            return []

        matrix = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(
            len(chunk_ids), dim
        )
        q = np.array(qvec, dtype=np.float32)
        # 向量已归一化，点积即余弦相似度
        scores = matrix @ q
        order = np.argsort(-scores)[: self.config.top_k]
        return [(chunk_ids[i], float(scores[i])) for i in order]

    def _keyword_search(
        self, query: str, source_ids: list[int] | None
    ) -> list[tuple[int, float]]:
        fts_query = _tokenize_for_fts(query)
        if not fts_query:
            return []
        rows = self.db.search_chunks_fts(
            fts_query, limit=self.config.top_k, source_ids=source_ids
        )
        # bm25 分数越小越相关，取负便于统一处理
        return [(int(r["id"]), -float(r["score"])) for r in rows]

    def _rrf_fuse(
        self,
        vector_hits: list[tuple[int, float]],
        keyword_hits: list[tuple[int, float]],
        *,
        k: int = 60,
    ) -> list[Hit]:
        """RRF 融合：score = Σ 1/(k + rank)。

        不用分数加权，因为余弦相似度与 BM25 分数量纲不可比。
        """
        scores: dict[int, float] = {}
        vrank: dict[int, int] = {}
        krank: dict[int, int] = {}

        for rank, (cid, _) in enumerate(vector_hits, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            vrank[cid] = rank
        for rank, (cid, _) in enumerate(keyword_hits, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            krank[cid] = rank

        if not scores:
            return []

        ordered = sorted(scores.items(), key=lambda kv: -kv[1])
        rows = {int(r["id"]): r for r in self.db.get_chunks_by_ids([c for c, _ in ordered])}

        hits: list[Hit] = []
        for cid, score in ordered:
            row = rows.get(cid)
            if row is None:
                continue
            hits.append(
                Hit(
                    chunk_id=cid,
                    source_id=int(row["source_id"]),
                    text=row["text"],
                    title=row["title"],
                    url=row["url"],
                    author=row["author"],
                    segment_idxs=json.loads(row["segment_idxs_json"] or "[]"),
                    score=score,
                    vector_rank=vrank.get(cid),
                    keyword_rank=krank.get(cid),
                )
            )
        return hits

    def _attach_anchors(self, hits: list[Hit]) -> None:
        """补上时间戳/章节锚点，使引用可跳转回原文位置。"""
        by_source: dict[int, list[Hit]] = {}
        for h in hits:
            by_source.setdefault(h.source_id, []).append(h)

        for source_id, group in by_source.items():
            segments = {s.idx: s for s in self.db.get_segments(source_id)}
            for hit in group:
                if not hit.segment_idxs:
                    continue
                seg = segments.get(hit.segment_idxs[0])
                if seg is not None:
                    hit.anchor = seg.anchor
