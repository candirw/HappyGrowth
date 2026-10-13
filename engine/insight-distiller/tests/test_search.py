"""检索与问答测试（M2）。

向量相关测试用 stub embedder，避免下载模型——
测试必须能离线快速跑完，否则 CI 会变得不可用。
"""

from __future__ import annotations

import sys

import pytest

from distiller.config import Config, ProviderConfig, RetrievalConfig, TaskConfig
from distiller.models import ContentType, RawContent, Segment, SourceStatus
from distiller.search.retriever import Retriever, _tokenize_for_fts
from distiller.store import Database

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import test_pipeline  # noqa: E402,F401 - 注册 fake LLM provider


class StubEmbedder:
    """确定性假向量：按字符哈希生成，相同文本必得相同向量。

    足以验证存储、检索、融合逻辑，且零下载零耗时。
    """

    dim = 16

    def __init__(self, *_a, **_kw) -> None:
        pass

    def _vec(self, text: str) -> list[float]:
        import math

        vals = [0.0] * self.dim
        for i, ch in enumerate(text):
            vals[(ord(ch) + i) % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vals)) or 1.0
        return [v / norm for v in vals]

    def encode(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def encode_one(self, text: str, *, is_query: bool = False) -> list[float]:
        return self._vec(text)


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture
def retriever(db: Database) -> Retriever:
    r = Retriever(db, RetrievalConfig(chunk_size=200, chunk_overlap=20, top_k=10, top_n=5))
    r._embedder = StubEmbedder()  # 注入 stub，避免下载真实模型
    return r


def _seed(db: Database, title: str, texts: list[str]) -> int:
    raw = RawContent(
        url=f"https://example.com/{title}",
        content_type=ContentType.ARTICLE,
        platform="test",
        title=title,
        segments=[
            Segment(idx=i, text=t, start_ms=i * 30_000, end_ms=(i + 1) * 30_000)
            for i, t in enumerate(texts)
        ],
    )
    from distiller.models import SourceStatus

    sid = db.upsert_source(raw, status=SourceStatus.DISTILLED)
    db.replace_segments(sid, raw.segments)
    return sid


class TestSchemaMigration:
    def test_fresh_db_is_latest_version(self, db: Database) -> None:
        from distiller.store import SCHEMA_VERSION

        assert int(db.get_meta("schema_version")) == SCHEMA_VERSION

    def test_m2_tables_exist(self, db: Database) -> None:
        """v2 迁移必须已应用，否则检索完全不可用。"""
        names = {
            r["name"]
            for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchall()
        }
        assert "embeddings" in names
        assert "chunks_fts" in names

    def test_migration_is_idempotent(self, tmp_path) -> None:
        """重复打开同一数据库不应报错或损坏数据。"""
        path = tmp_path / "idem.db"
        d1 = Database(path)
        sid = _seed(d1, "文章", ["测试内容一段"])
        d1.close()

        d2 = Database(path)
        assert d2.get_source(sid) is not None
        d2.close()


class TestIndexing:
    def test_index_and_count(self, db: Database, retriever: Retriever) -> None:
        sid = _seed(db, "向量检索", ["检索增强生成需要混合召回。" * 8, "BM25 补充专有名词召回。" * 8])
        n = retriever.index_source(sid, db.get_segments(sid))
        assert n > 0
        assert db.count_indexed(retriever.config.embedding_model) == n

    def test_pending_list_shrinks_after_index(self, db: Database, retriever: Retriever) -> None:
        sid = _seed(db, "待索引", ["一些内容。" * 20])
        model = retriever.config.embedding_model
        assert sid in db.sources_without_index(model)
        retriever.index_source(sid, db.get_segments(sid))
        assert sid not in db.sources_without_index(model)

    def test_reindex_replaces_not_duplicates(self, db: Database, retriever: Retriever) -> None:
        """重建索引不能产生重复向量，否则检索结果会被同一内容淹没。"""
        sid = _seed(db, "重建", ["内容内容内容。" * 20])
        first = retriever.index_source(sid, db.get_segments(sid))
        second = retriever.index_source(sid, db.get_segments(sid))
        assert first == second
        assert db.count_indexed(retriever.config.embedding_model) == first


class TestHybridSearch:
    def test_finds_relevant_chunk(self, db: Database, retriever: Retriever) -> None:
        sid = _seed(
            db,
            "混合检索",
            [
                "向量检索在专有名词和数字上召回率较差，需要引入 BM25 做补充。" * 3,
                "说话人分离是整条链路里最慢的一环，通常只在访谈类内容开启。" * 3,
            ],
        )
        retriever.index_source(sid, db.get_segments(sid))
        hits = retriever.search("BM25 专有名词召回")
        assert hits
        assert any("BM25" in h.text for h in hits)

    def test_hits_carry_anchor_for_traceability(self, db: Database, retriever: Retriever) -> None:
        """命中必须带锚点，否则回答无法回溯原文位置。"""
        sid = _seed(db, "锚点", ["第一段内容用于验证时间戳锚点。" * 10])
        retriever.index_source(sid, db.get_segments(sid))
        hits = retriever.search("时间戳锚点")
        assert hits
        assert hits[0].anchor is not None
        assert hits[0].cite.startswith(f"S{sid}")

    def test_empty_index_returns_no_hits(self, retriever: Retriever) -> None:
        assert retriever.search("任何问题") == []

    def test_source_filter_restricts_scope(self, db: Database, retriever: Retriever) -> None:
        a = _seed(db, "甲文", ["混合检索需要 BM25 与向量两路召回。" * 5])
        b = _seed(db, "乙文", ["说话人分离很慢，一般不开启。" * 5])
        retriever.index_source(a, db.get_segments(a))
        retriever.index_source(b, db.get_segments(b))

        hits = retriever.search("BM25 召回", source_ids=[b])
        assert all(h.source_id == b for h in hits)


class TestFtsTokenizer:
    def test_strips_fts_special_chars(self) -> None:
        """FTS5 对引号星号等敏感，用户随意输入不能导致查询报错。"""
        q = _tokenize_for_fts('这是 "引号" 和 * 星号 (括号)')
        assert "*" not in q
        assert "(" not in q

    def test_expands_chinese_bigrams(self) -> None:
        # unicode61 分词器对中文长词命中率低，需切二元组
        q = _tokenize_for_fts("检索增强生成")
        assert "检索" in q

    def test_empty_query(self) -> None:
        assert _tokenize_for_fts("!!!") == ""

    def test_index_and_query_share_impl(self) -> None:
        """索引侧与查询侧必须用同一套切分。

        两边各写一份正是中文 BM25 曾经完全失效的原因。
        """
        from distiller.store.fts import index_text, query_text

        # 同一个词，索引侧产出的 token 必须能被查询侧的表达式命中
        indexed = index_text("去了一趟山西")
        assert "山西" in indexed.split()
        assert '"山西"' in query_text("山西")


class TestChineseBm25:
    """中文关键词检索的端到端验证。

    回归测试：v4 之前索引侧存原文、查询侧切二元组，两边对不上，
    导致 FTS MATCH「山西」在正文含「山西」时返回 0 条——
    混合检索的关键词一路对中文**完全失效**，而且不报错、
    只是静默少一路召回，极难察觉。
    """

    @pytest.fixture
    def db(self, tmp_path) -> Database:
        from distiller.distill.chunker import Chunk

        database = Database(tmp_path / "fts.db")
        raw = RawContent(
            url="https://example.com/sx",
            content_type=ContentType.ARTICLE,
            platform="web",
            title="去了一趟山西",
            segments=[Segment(idx=0, text="山西的古建与美食")],
        )
        sid = database.upsert_source(raw, status=SourceStatus.FETCHED)
        database.replace_chunks(
            sid,
            [
                Chunk(
                    idx=0,
                    text="这一次我们去了一趟历史底蕴深厚煤炭资源丰富的山西，"
                    "想带你尝尝地道的山西美食，也看看折叠屏手机拍古建的效果。",
                    segment_idxs=[0],
                )
            ],
        )
        yield database
        database.close()

    @pytest.mark.parametrize("term", ["山西", "美食", "煤炭", "折叠", "古建"])
    def test_two_char_chinese_terms_hit(self, db: Database, term: str) -> None:
        """2 字中文词是最常见的查询形态，必须能命中。

        这也是不能用 trigram 分词器的原因——它要求查询至少 3 字符。
        """
        rows = db.search_chunks_fts(_tokenize_for_fts(term), limit=5)
        assert rows, f"「{term}」应命中但返回 0 条"

    def test_longer_phrase_hits(self, db: Database) -> None:
        assert db.search_chunks_fts(_tokenize_for_fts("山西美食"), limit=5)

    def test_absent_term_does_not_hit(self, db: Database) -> None:
        """不能为了召回率而误召——查库里没有的词必须返回空。"""
        assert not db.search_chunks_fts(_tokenize_for_fts("量子计算"), limit=5)

    def test_english_still_works(self, db: Database) -> None:
        from distiller.distill.chunker import Chunk

        raw = RawContent(
            url="https://example.com/en",
            content_type=ContentType.ARTICLE,
            platform="web",
            title="RAG",
            segments=[Segment(idx=0, text="x")],
        )
        sid = db.upsert_source(raw, status=SourceStatus.FETCHED)
        db.replace_chunks(
            sid, [Chunk(idx=0, text="RAG combines BM25 and embeddings.", segment_idxs=[0])]
        )
        assert db.search_chunks_fts(_tokenize_for_fts("BM25"), limit=5)
        # 英文大小写不应影响命中
        assert db.search_chunks_fts(_tokenize_for_fts("bm25"), limit=5)

    def test_rebuild_fts_restores_index(self, db: Database) -> None:
        """旧库升级后 FTS 为空，rebuild 必须能恢复。"""
        db.conn.execute("DELETE FROM chunks_fts")
        db.conn.commit()
        assert not db.search_chunks_fts(_tokenize_for_fts("山西"), limit=5)

        n = db.rebuild_fts()
        assert n >= 1
        assert db.search_chunks_fts(_tokenize_for_fts("山西"), limit=5)

    def test_replace_chunks_cleans_stale_fts(self, db: Database) -> None:
        """重新入库时旧 FTS 记录必须删除，否则会召回已不存在的内容。"""
        from distiller.distill.chunker import Chunk

        sid = db.list_sources(limit=1)[0].id
        db.replace_chunks(
            sid, [Chunk(idx=0, text="内容已经完全换成了深圳的城市规划", segment_idxs=[0])]
        )
        assert not db.search_chunks_fts(_tokenize_for_fts("山西"), limit=5)
        assert db.search_chunks_fts(_tokenize_for_fts("深圳"), limit=5)

    def test_needs_rebuild_flag(self, db: Database) -> None:
        db.set_meta("fts_needs_rebuild", "1")
        assert db.fts_needs_rebuild()
        db.rebuild_fts()
        assert not db.fts_needs_rebuild()


class TestChat:
    def test_no_hits_does_not_fabricate(self, db: Database) -> None:
        """空库时必须明确说没有依据，不能硬答——这是可信度的底线。"""
        from distiller.search import ChatEngine

        cfg = Config(
            providers={"fake": ProviderConfig(type="fake", models={"chat": "f"})},
            tasks={"qa": TaskConfig(provider="fake")},
            retrieval=RetrievalConfig(),
        )
        engine = ChatEngine(cfg, db)
        engine.retriever._embedder = StubEmbedder()
        answer = engine.ask("库里没有的主题")
        assert not answer.hits
        assert "没有检索到" in answer.text

    def test_answer_persists_to_session(self, db: Database, retriever: Retriever) -> None:
        from distiller.search import ChatEngine

        sid = _seed(db, "会话", ["混合检索结合向量与关键词两路召回。" * 6])
        retriever.index_source(sid, db.get_segments(sid))

        cfg = Config(
            providers={"fake": ProviderConfig(type="fake", models={"chat": "f"})},
            tasks={"qa": TaskConfig(provider="fake")},
            retrieval=retriever.config,
        )
        engine = ChatEngine(cfg, db)
        engine.retriever._embedder = StubEmbedder()
        answer = engine.ask("混合检索是什么")

        assert answer.hits
        history = db.get_session(answer.session_id)
        assert len(history) == 2
        assert history[0]["role"] == "user"
