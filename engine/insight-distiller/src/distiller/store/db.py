"""数据库访问层。

跨平台要点：
- WAL 模式 + busy_timeout：Windows 文件锁比 macOS 严格，不设置会频繁 database is locked。
- 连接不跨线程复用（check_same_thread 保持默认 True）。
- 时间统一 UTC ISO8601 存储。
- 文件路径只存相对路径，保证数据目录可跨平台拷贝。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..compat.fsio import ENCODING, read_text
from . import fts
from ..models import (
    ContentType,
    Digest,
    RawContent,
    Segment,
    Source,
    SourceStatus,
    utcnow,
)

SCHEMA_VERSION = 4
_SCHEMA_FILE = Path(__file__).with_name("schema.sql")
_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        # Windows 上锁竞争更明显，显式设置等待而非立刻报错
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    # ---------- schema 管理 ----------

    def _migrate(self) -> None:
        """建表并按需升级。

        新库：执行 schema.sql（v1 基线）后依次应用所有迁移脚本。
        老库：只应用缺失版本的迁移，升级前自动备份。
        迁移脚本全部使用 IF NOT EXISTS，因此重复执行是安全的。
        """
        fresh = self.get_meta_safe("schema_version") is None
        self.conn.executescript(read_text(_SCHEMA_FILE))

        current = self.get_meta("schema_version")
        if fresh or current is None:
            self._apply_migrations(1)
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        elif int(current) < SCHEMA_VERSION:
            backup = self.path.with_suffix(f".backup-v{current}.db")
            try:
                shutil.copy2(self.path, backup)
            except OSError:
                pass  # 备份失败不阻断升级，迁移本身是幂等的
            self._apply_migrations(int(current))
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        self.conn.commit()

    def _apply_migrations(self, from_version: int) -> None:
        if not _MIGRATIONS_DIR.exists():
            return
        for version in range(from_version + 1, SCHEMA_VERSION + 1):
            script = _MIGRATIONS_DIR / f"v{version}.sql"
            if script.exists():
                self.conn.executescript(read_text(script))

    def get_meta_safe(self, key: str) -> str | None:
        """meta 表尚不存在时也能安全查询（用于判断是否为新库）。"""
        try:
            return self.get_meta(key)
        except sqlite3.Error:
            return None

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    # ---------- sources ----------

    def find_source_by_url(self, url: str) -> Source | None:
        row = self.conn.execute("SELECT * FROM sources WHERE url = ?", (url,)).fetchone()
        return self._row_to_source(row) if row else None

    def upsert_source(self, raw: RawContent, *, status: SourceStatus) -> int:
        now = _iso(utcnow())
        existing = self.find_source_by_url(raw.url)
        if existing:
            self.conn.execute(
                "UPDATE sources SET title=?, author=?, publish_at=?, duration_sec=?, "
                "lang=?, status=?, audio_rel_path=?, warnings_json=?, extra_json=?, "
                "updated_at=? WHERE id=?",
                (
                    raw.title, raw.author, _iso(raw.publish_at), raw.duration_sec,
                    raw.lang, status.value, raw.audio_rel_path,
                    json.dumps(raw.warnings, ensure_ascii=False),
                    json.dumps(raw.extra, ensure_ascii=False),
                    now, existing.id,
                ),
            )
            self.conn.commit()
            return int(existing.id)

        cur = self.conn.execute(
            "INSERT INTO sources(content_type, platform, url, title, author, publish_at, "
            "duration_sec, lang, status, audio_rel_path, warnings_json, extra_json, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                raw.content_type.value, raw.platform, raw.url, raw.title, raw.author,
                _iso(raw.publish_at), raw.duration_sec, raw.lang, status.value,
                raw.audio_rel_path,
                json.dumps(raw.warnings, ensure_ascii=False),
                json.dumps(raw.extra, ensure_ascii=False),
                now, now,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_status(self, source_id: int, status: SourceStatus) -> None:
        self.conn.execute(
            "UPDATE sources SET status=?, updated_at=? WHERE id=?",
            (status.value, _iso(utcnow()), source_id),
        )
        self.conn.commit()

    def set_density(self, source_id: int, score: float | None) -> None:
        self.conn.execute(
            "UPDATE sources SET density_score=?, updated_at=? WHERE id=?",
            (score, _iso(utcnow()), source_id),
        )
        self.conn.commit()

    def get_source(self, source_id: int) -> Source | None:
        row = self.conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return self._row_to_source(row) if row else None

    def list_sources(
        self,
        *,
        limit: int = 50,
        content_type: str | None = None,
        order_by: str = "created_at",
        min_density: float | None = None,
    ) -> list[Source]:
        # 白名单防注入：排序字段不能来自用户输入的裸拼接
        allowed = {"created_at", "density_score", "publish_at", "title"}
        if order_by not in allowed:
            order_by = "created_at"

        sql = "SELECT * FROM sources WHERE 1=1"
        params: list[Any] = []
        if content_type:
            sql += " AND content_type = ?"
            params.append(content_type)
        if min_density is not None:
            sql += " AND density_score >= ?"
            params.append(min_density)
        sql += f" ORDER BY {order_by} DESC NULLS LAST LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_source(r) for r in rows]

    @staticmethod
    def _row_to_source(row: sqlite3.Row) -> Source:
        return Source(
            id=row["id"],
            content_type=ContentType(row["content_type"]),
            platform=row["platform"],
            url=row["url"],
            title=row["title"],
            author=row["author"],
            publish_at=_parse_dt(row["publish_at"]),
            duration_sec=row["duration_sec"],
            lang=row["lang"],
            status=SourceStatus(row["status"]),
            density_score=row["density_score"],
            domains=json.loads(row["domains_json"] or "[]"),
            created_at=_parse_dt(row["created_at"]) or utcnow(),
        )

    # ---------- segments ----------

    def replace_segments(self, source_id: int, segments: Iterable[Segment]) -> None:
        self.conn.execute("DELETE FROM segments WHERE source_id = ?", (source_id,))
        self.conn.executemany(
            "INSERT INTO segments(source_id, idx, start_ms, end_ms, section, text) "
            "VALUES(?,?,?,?,?,?)",
            [
                (source_id, s.idx, s.start_ms, s.end_ms, s.section, s.text)
                for s in segments
            ],
        )
        self.conn.commit()

    def get_segments(self, source_id: int) -> list[Segment]:
        rows = self.conn.execute(
            "SELECT idx, start_ms, end_ms, section, text FROM segments "
            "WHERE source_id = ? ORDER BY idx",
            (source_id,),
        ).fetchall()
        return [
            Segment(
                idx=r["idx"], start_ms=r["start_ms"], end_ms=r["end_ms"],
                section=r["section"], text=r["text"],
            )
            for r in rows
        ]

    def search_segments(self, query: str, *, limit: int = 20) -> list[sqlite3.Row]:
        """FTS5 全文检索。混合检索的 BM25 一路。"""
        return self.conn.execute(
            "SELECT s.source_id, s.idx, s.text, src.title, "
            "       bm25(segments_fts) AS score "
            "FROM segments_fts "
            "JOIN segments s ON s.id = segments_fts.rowid "
            "JOIN sources src ON src.id = s.source_id "
            "WHERE segments_fts MATCH ? ORDER BY score LIMIT ?",
            (query, limit),
        ).fetchall()

    # ---------- digests ----------

    def save_digest(self, source_id: int, digest: Digest) -> int:
        cur = self.conn.execute(
            "INSERT INTO digests(source_id, schema_version, prompt_version, payload_json, "
            "model, tokens_in, tokens_out, cost_usd, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                source_id, digest.schema_version, digest.prompt_version,
                digest.model_dump_json(exclude={"model_used", "cost_usd", "tokens_in", "tokens_out"}),
                digest.model_used, digest.tokens_in, digest.tokens_out,
                digest.cost_usd, _iso(utcnow()),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_latest_digest(self, source_id: int) -> Digest | None:
        row = self.conn.execute(
            "SELECT * FROM digests WHERE source_id = ? ORDER BY id DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload_json"])
        payload.update(
            model_used=row["model"],
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
            cost_usd=row["cost_usd"],
        )
        return Digest.model_validate(payload)

    # ---------- chunks 与向量（M2） ----------

    def replace_chunks(self, source_id: int, chunks: list) -> list[int]:
        """写入检索用 chunk，返回其数据库 id（顺序与输入一致）。

        FTS 由这里显式维护而非触发器：中文需要二元组切分，
        而 SQL 触发器里做不了字符串切分（v4 之前正因如此，
        索引侧存的是原文，中文 BM25 完全失效）。
        """
        old = self.conn.execute(
            "SELECT id FROM chunks WHERE source_id = ?", (source_id,)
        ).fetchall()
        for row in old:
            self.conn.execute(
                "DELETE FROM chunks_fts WHERE rowid = ?", (int(row["id"]),)
            )
        self.conn.execute("DELETE FROM chunks WHERE source_id = ?", (source_id,))

        ids: list[int] = []
        for c in chunks:
            cur = self.conn.execute(
                "INSERT INTO chunks(source_id, idx, text, segment_idxs_json, token_count) "
                "VALUES(?,?,?,?,?)",
                (
                    source_id, c.idx, c.text,
                    json.dumps(c.segment_idxs), len(c.text),
                ),
            )
            chunk_id = int(cur.lastrowid)
            self.conn.execute(
                "INSERT INTO chunks_fts(rowid, text) VALUES(?, ?)",
                (chunk_id, fts.index_text(c.text)),
            )
            ids.append(chunk_id)
        self.conn.commit()
        return ids

    def rebuild_fts(self) -> int:
        """按当前切分规则重建全部 FTS 索引。返回重建的 chunk 数。

        两种场景需要：① 旧库升级到 v4；② 切分规则调整后。
        """
        self.conn.execute("DELETE FROM chunks_fts")
        rows = self.conn.execute("SELECT id, text FROM chunks").fetchall()
        self.conn.executemany(
            "INSERT INTO chunks_fts(rowid, text) VALUES(?, ?)",
            [(int(r["id"]), fts.index_text(r["text"])) for r in rows],
        )
        self.set_meta("fts_needs_rebuild", "0")
        self.conn.commit()
        return len(rows)

    def fts_needs_rebuild(self) -> bool:
        """FTS 是否待重建。v4 迁移会置位，提示用户关键词检索暂时失效。"""
        if self.get_meta_safe("fts_needs_rebuild") != "1":
            return False
        # 库里没有 chunk 时无所谓重建
        n = self.conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        return int(n) > 0

    def save_embeddings(
        self,
        *,
        source_id: int,
        chunk_ids: list[int],
        vectors: list[list[float]],
        model: str,
    ) -> None:
        """存向量。float32 二进制存 BLOB，比 JSON 省空间且解析快。"""
        import struct

        now = _iso(utcnow())
        rows = []
        for chunk_id, vec in zip(chunk_ids, vectors):
            blob = struct.pack(f"<{len(vec)}f", *vec)
            rows.append((chunk_id, source_id, model, len(vec), blob, now))

        self.conn.executemany(
            "INSERT INTO embeddings(chunk_id, source_id, model, dim, vector, created_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(chunk_id) DO UPDATE SET "
            "model=excluded.model, dim=excluded.dim, vector=excluded.vector",
            rows,
        )
        self.conn.commit()

    def load_all_embeddings(
        self, model: str, *, source_ids: list[int] | None = None
    ) -> tuple[list[int], list[bytes], int]:
        """加载向量用于检索。返回 (chunk_ids, blobs, dim)。"""
        sql = "SELECT chunk_id, vector, dim FROM embeddings WHERE model = ?"
        params: list[Any] = [model]
        if source_ids:
            placeholders = ",".join("?" * len(source_ids))
            sql += f" AND source_id IN ({placeholders})"
            params.extend(source_ids)

        rows = self.conn.execute(sql, params).fetchall()
        if not rows:
            return [], [], 0
        return (
            [r["chunk_id"] for r in rows],
            [r["vector"] for r in rows],
            int(rows[0]["dim"]),
        )

    def get_chunks_by_ids(self, chunk_ids: list[int]) -> list[sqlite3.Row]:
        if not chunk_ids:
            return []
        placeholders = ",".join("?" * len(chunk_ids))
        return self.conn.execute(
            f"SELECT c.id, c.source_id, c.idx, c.text, c.segment_idxs_json, "
            f"       s.title, s.url, s.content_type, s.author "
            f"FROM chunks c JOIN sources s ON s.id = c.source_id "
            f"WHERE c.id IN ({placeholders})",
            chunk_ids,
        ).fetchall()

    def search_chunks_fts(
        self, query: str, *, limit: int = 30, source_ids: list[int] | None = None
    ) -> list[sqlite3.Row]:
        """chunk 级 BM25 检索。混合召回的关键词一路。"""
        sql = (
            "SELECT c.id, c.source_id, c.idx, c.text, c.segment_idxs_json, "
            "       s.title, s.url, s.content_type, s.author, "
            "       bm25(chunks_fts) AS score "
            "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
            "JOIN sources s ON s.id = c.source_id "
            "WHERE chunks_fts MATCH ?"
        )
        params: list[Any] = [query]
        if source_ids:
            placeholders = ",".join("?" * len(source_ids))
            sql += f" AND c.source_id IN ({placeholders})"
            params.extend(source_ids)
        sql += " ORDER BY score LIMIT ?"
        params.append(limit)

        try:
            return self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # FTS5 查询语法错误（用户输入含特殊字符）不应导致整体失败
            return []

    def count_indexed(self, model: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM embeddings WHERE model = ?", (model,)
        ).fetchone()
        return int(row["n"])

    def sources_without_index(self, model: str) -> list[int]:
        rows = self.conn.execute(
            "SELECT DISTINCT s.id FROM sources s "
            "WHERE s.status = 'distilled' AND NOT EXISTS ("
            "  SELECT 1 FROM embeddings e WHERE e.source_id = s.id AND e.model = ?"
            ")",
            (model,),
        ).fetchall()
        return [int(r["id"]) for r in rows]

    # ---------- 问答历史 ----------

    def save_message(
        self, *, session_id: str, source_ids: list[int], role: str,
        content: str, citations: list[dict] | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO messages(session_id, source_ids_json, role, content, "
            "citations_json, created_at) VALUES(?,?,?,?,?,?)",
            (
                session_id, json.dumps(source_ids), role, content,
                json.dumps(citations or [], ensure_ascii=False), _iso(utcnow()),
            ),
        )
        self.conn.commit()

    def get_session(self, session_id: str, *, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()[::-1]

    # ---------- 实体（M3.5） ----------

    def upsert_entity(
        self,
        name: str,
        *,
        etype: str | None = None,
        definition: str | None = None,
        aliases: list[str] | None = None,
    ) -> int:
        """插入或更新实体。mention_count 由 add_mention 累加。"""
        row = self.conn.execute(
            "SELECT id, aliases_json, definition FROM entities WHERE name = ?", (name,)
        ).fetchone()
        if row:
            merged = sorted(set(json.loads(row["aliases_json"] or "[]")) | set(aliases or []))
            self.conn.execute(
                "UPDATE entities SET type = COALESCE(?, type), "
                "definition = COALESCE(definition, ?), aliases_json = ? WHERE id = ?",
                (etype, definition, json.dumps(merged, ensure_ascii=False), row["id"]),
            )
            self.conn.commit()
            return int(row["id"])

        cur = self.conn.execute(
            "INSERT INTO entities(name, type, aliases_json, definition, mention_count) "
            "VALUES(?,?,?,?,0)",
            (name, etype, json.dumps(aliases or [], ensure_ascii=False), definition),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def add_mention(
        self, entity_id: int, source_id: int, *, segment_id: int | None = None,
        context: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO entity_mentions(entity_id, source_id, segment_id, context) "
            "VALUES(?,?,?,?)",
            (entity_id, source_id, segment_id or 0, context),
        )
        self.conn.execute(
            "UPDATE entities SET mention_count = ("
            "  SELECT COUNT(DISTINCT source_id) FROM entity_mentions WHERE entity_id = ?"
            ") WHERE id = ?",
            (entity_id, entity_id),
        )
        self.conn.commit()

    def find_entity(self, name: str) -> sqlite3.Row | None:
        """按名称或别名查实体。"""
        row = self.conn.execute(
            "SELECT * FROM entities WHERE name = ?", (name,)
        ).fetchone()
        if row:
            return row
        # 别名匹配：JSON 数组里做包含判断
        return self.conn.execute(
            "SELECT * FROM entities WHERE aliases_json LIKE ?", (f'%"{name}"%',)
        ).fetchone()

    def list_entities(
        self, *, min_mentions: int = 1, limit: int = 50, etype: str | None = None
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM entities WHERE mention_count >= ?"
        params: list[Any] = [min_mentions]
        if etype:
            sql += " AND type = ?"
            params.append(etype)
        sql += " ORDER BY mention_count DESC, name LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def entity_sources(self, entity_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT DISTINCT s.id, s.title, s.content_type, s.url, m.context "
            "FROM entity_mentions m JOIN sources s ON s.id = m.source_id "
            "WHERE m.entity_id = ? ORDER BY s.id",
            (entity_id,),
        ).fetchall()

    def shared_entity_sources(
        self, source_id: int, *, min_shared: int = 2, limit: int = 20
    ) -> list[sqlite3.Row]:
        """找出与该内容共享实体最多的其他内容。

        这是关联漏斗的第一级：纯 SQL join，零成本零 LLM 调用。
        """
        return self.conn.execute(
            "SELECT m2.source_id, COUNT(DISTINCT m2.entity_id) AS shared, "
            "       GROUP_CONCAT(DISTINCT e.name) AS entities "
            "FROM entity_mentions m1 "
            "JOIN entity_mentions m2 ON m1.entity_id = m2.entity_id "
            "JOIN entities e ON e.id = m1.entity_id "
            "WHERE m1.source_id = ? AND m2.source_id != ? "
            "GROUP BY m2.source_id HAVING shared >= ? "
            "ORDER BY shared DESC LIMIT ?",
            (source_id, source_id, min_shared, limit),
        ).fetchall()

    def prune_entities(self, *, min_mentions: int = 2) -> int:
        """清理只出现一次的实体，防止图谱被垃圾实体淹没。"""
        cur = self.conn.execute(
            "DELETE FROM entities WHERE mention_count < ?", (min_mentions,)
        )
        self.conn.commit()
        return cur.rowcount

    def merge_entities(self, keep_id: int, merge_id: int) -> None:
        """合并两个实体（同一概念的不同写法）。"""
        keep = self.conn.execute(
            "SELECT name, aliases_json FROM entities WHERE id = ?", (keep_id,)
        ).fetchone()
        drop = self.conn.execute(
            "SELECT name, aliases_json FROM entities WHERE id = ?", (merge_id,)
        ).fetchone()
        if not keep or not drop:
            return

        aliases = set(json.loads(keep["aliases_json"] or "[]"))
        aliases |= set(json.loads(drop["aliases_json"] or "[]"))
        aliases.add(drop["name"])
        aliases.discard(keep["name"])

        self.conn.execute(
            "UPDATE OR IGNORE entity_mentions SET entity_id = ? WHERE entity_id = ?",
            (keep_id, merge_id),
        )
        self.conn.execute("DELETE FROM entity_mentions WHERE entity_id = ?", (merge_id,))
        self.conn.execute(
            "UPDATE entities SET aliases_json = ? WHERE id = ?",
            (json.dumps(sorted(aliases), ensure_ascii=False), keep_id),
        )
        self.conn.execute("DELETE FROM entities WHERE id = ?", (merge_id,))
        self.conn.execute(
            "UPDATE entities SET mention_count = ("
            "  SELECT COUNT(DISTINCT source_id) FROM entity_mentions WHERE entity_id = ?"
            ") WHERE id = ?",
            (keep_id, keep_id),
        )
        self.conn.commit()

    # ---------- 关系（M3.5） ----------

    def add_relation(
        self,
        from_source: int,
        to_source: int,
        rel_type: str,
        *,
        confidence: float = 0.0,
        evidence: dict | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO relations(from_source, to_source, rel_type, confidence, "
            "evidence_json, created_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(from_source, to_source, rel_type) DO UPDATE SET "
            "confidence = MAX(confidence, excluded.confidence), "
            "evidence_json = excluded.evidence_json",
            (
                from_source, to_source, rel_type, confidence,
                json.dumps(evidence or {}, ensure_ascii=False), _iso(utcnow()),
            ),
        )
        self.conn.commit()

    def get_relations(
        self, source_id: int, *, min_confidence: float = 0.0, limit: int = 20
    ) -> list[sqlite3.Row]:
        """取与该内容相关的所有关系（双向）。"""
        return self.conn.execute(
            "SELECT r.*, "
            "  CASE WHEN r.from_source = ? THEN r.to_source ELSE r.from_source END AS other_id, "
            "  CASE WHEN r.from_source = ? THEN 'out' ELSE 'in' END AS direction, "
            "  s.title AS other_title, s.content_type AS other_type, s.url AS other_url "
            "FROM relations r JOIN sources s ON s.id = "
            "  CASE WHEN r.from_source = ? THEN r.to_source ELSE r.from_source END "
            "WHERE (r.from_source = ? OR r.to_source = ?) AND r.confidence >= ? "
            "  AND (r.verified_by_user IS NULL OR r.verified_by_user = 1) "
            "ORDER BY r.confidence DESC LIMIT ?",
            (source_id, source_id, source_id, source_id, source_id, min_confidence, limit),
        ).fetchall()

    def verify_relation(self, relation_id: int, *, accepted: bool) -> None:
        """用户确认或否决关系判定。被否决的不再展示。"""
        self.conn.execute(
            "UPDATE relations SET verified_by_user = ? WHERE id = ?",
            (1 if accepted else 0, relation_id),
        )
        self.conn.commit()

    def relation_exists(self, from_source: int, to_source: int, rel_type: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM relations WHERE from_source = ? AND to_source = ? "
            "AND rel_type = ?",
            (from_source, to_source, rel_type),
        ).fetchone()
        return row is not None

    def graph_neighbors(
        self, source_id: int, *, depth: int = 2, limit: int = 40
    ) -> list[sqlite3.Row]:
        """递归 CTE 做多跳遍历。

        个人场景关系边最多几万条，SQLite 递归查询足够；
        引入图数据库是过度设计（见 KNOWLEDGE.md）。
        """
        return self.conn.execute(
            """
            WITH RECURSIVE walk(node, dist, path) AS (
                SELECT ?, 0, ',' || ? || ','
                UNION
                SELECT CASE WHEN r.from_source = w.node THEN r.to_source
                            ELSE r.from_source END,
                       w.dist + 1,
                       w.path || CASE WHEN r.from_source = w.node THEN r.to_source
                                      ELSE r.from_source END || ','
                FROM relations r JOIN walk w
                  ON (r.from_source = w.node OR r.to_source = w.node)
                WHERE w.dist < ?
                  AND (r.verified_by_user IS NULL OR r.verified_by_user = 1)
                  AND w.path NOT LIKE '%,' || CASE WHEN r.from_source = w.node
                        THEN r.to_source ELSE r.from_source END || ',%'
            )
            SELECT w.node AS id, MIN(w.dist) AS dist, s.title, s.content_type,
                   s.density_score
            FROM walk w JOIN sources s ON s.id = w.node
            WHERE w.node != ?
            GROUP BY w.node ORDER BY dist, s.id LIMIT ?
            """,
            (source_id, source_id, depth, source_id, limit),
        ).fetchall()

    # ---------- Claim（M3.5 抽取，M6 供 agent 消费） ----------

    def replace_claims(self, source_id: int, claims: list[dict]) -> int:
        self.conn.execute("DELETE FROM claims WHERE source_id = ?", (source_id,))
        now = _iso(utcnow())
        rows = [
            (
                source_id,
                json.dumps(c.get("segment_idxs") or []),
                c.get("domain"),
                c.get("subject"),
                c.get("predicate", ""),
                c.get("claim_type", "opinion"),
                c.get("polarity"),
                c.get("confidence"),
                c.get("as_of"),
                c.get("expires_hint"),
                json.dumps(c.get("numeric") or {}, ensure_ascii=False),
                json.dumps(c.get("conditions") or {}, ensure_ascii=False),
                c.get("authority"),
                json.dumps(c.get("contradicts") or []),
                now,
            )
            for c in claims
            if c.get("predicate")
        ]
        self.conn.executemany(
            "INSERT INTO claims(source_id, segment_idxs_json, domain, subject, predicate, "
            "claim_type, polarity, confidence, as_of, expires_hint, numeric_json, "
            "conditions_json, authority, contradicts_json, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def search_claims(
        self,
        *,
        domain: str | None = None,
        claim_types: list[str] | None = None,
        subject: str | None = None,
        limit: int = 30,
    ) -> list[sqlite3.Row]:
        sql = (
            "SELECT c.*, s.title, s.url, s.author, s.platform, "
            "s.authority AS source_authority, s.publish_at "
            "FROM claims c JOIN sources s ON s.id = c.source_id WHERE 1=1"
        )
        params: list[Any] = []
        if domain:
            sql += " AND c.domain = ?"
            params.append(domain)
        if claim_types:
            placeholders = ",".join("?" * len(claim_types))
            sql += f" AND c.claim_type IN ({placeholders})"
            params.extend(claim_types)
        if subject:
            # 按词切分而非整串 LIKE。
            # 整串匹配时「国债 利率」这类多词查询必然失配——
            # 没有哪条 claim 的文本里会同时按此顺序出现这两个词。
            terms = [t for t in str(subject).replace("，", " ").split() if t][:6]
            if terms:
                clauses = " OR ".join(
                    ["(c.subject LIKE ? OR c.predicate LIKE ?)"] * len(terms)
                )
                sql += f" AND ({clauses})"
                for term in terms:
                    params.extend([f"%{term}%", f"%{term}%"])
        sql += " ORDER BY c.confidence DESC NULLS LAST, c.id DESC LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def count_claims(self, *, domain: str | None = None) -> int:
        if domain:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM claims WHERE domain = ?", (domain,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM claims").fetchone()
        return int(row["n"])

    def sources_without_claims(self, *, min_density: float = 0.0) -> list[int]:
        rows = self.conn.execute(
            "SELECT s.id FROM sources s WHERE s.status = 'distilled' "
            "AND COALESCE(s.density_score, 0) >= ? "
            "AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.source_id = s.id)",
            (min_density,),
        ).fetchall()
        return [int(r["id"]) for r in rows]

    # ---------- 疑问追踪（M3.5 记录，M4 闭环） ----------

    def add_question(self, text: str, *, source_id: int | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO questions(source_id, text, status, created_at) "
            "VALUES(?,?,'open',?)",
            (source_id, text, _iso(utcnow())),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def open_questions(self, *, limit: int = 50) -> list[sqlite3.Row]:
        """未解疑问。按文本去重——不同内容常留下相同疑问，重复展示没有意义。"""
        return self.conn.execute(
            "SELECT q.*, s.title AS source_title FROM questions q "
            "LEFT JOIN sources s ON s.id = q.source_id "
            "WHERE q.status = 'open' "
            "AND q.id IN (SELECT MIN(id) FROM questions WHERE status = 'open' GROUP BY text) "
            "ORDER BY q.id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def answer_question(self, question_id: int, source_id: int) -> None:
        self.conn.execute(
            "UPDATE questions SET status = 'answered', answered_by_source = ? "
            "WHERE id = ?",
            (source_id, question_id),
        )
        self.conn.commit()

    # ---------- 知识盲区（M3.5） ----------

    def concept_gaps(self, *, limit: int = 20) -> list[sqlite3.Row]:
        """被反复提及但缺少定义的概念 —— 你的知识缺口。"""
        return self.conn.execute(
            "SELECT name, type, mention_count FROM entities "
            "WHERE mention_count >= 2 AND (definition IS NULL OR definition = '') "
            "ORDER BY mention_count DESC LIMIT ?",
            (limit,),
        ).fetchall()

    # ---------- 批注（M4） ----------

    def add_note(
        self,
        source_id: int,
        text: str,
        *,
        note_type: str = "comment",
        segment_id: int | None = None,
    ) -> int:
        """记录我的批注。

        note_type 区分用途：comment（想法）/ todo（待办）/ disagree（不同意）/
        insight（自己的洞察）。区分是为了让每周简报能分别汇总。
        """
        cur = self.conn.execute(
            "INSERT INTO notes(source_id, segment_id, text, note_type, created_at) "
            "VALUES(?,?,?,?,?)",
            (source_id, segment_id, text, note_type, _iso(utcnow())),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_notes(
        self, source_id: int, *, note_type: str | None = None
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM notes WHERE source_id = ?"
        params: list[Any] = [source_id]
        if note_type:
            sql += " AND note_type = ?"
            params.append(note_type)
        sql += " ORDER BY id"
        return self.conn.execute(sql, params).fetchall()

    def recent_notes(
        self, *, since: str | None = None, note_type: str | None = None, limit: int = 50
    ) -> list[sqlite3.Row]:
        sql = (
            "SELECT n.*, s.title FROM notes n JOIN sources s ON s.id = n.source_id "
            "WHERE 1=1"
        )
        params: list[Any] = []
        if since:
            sql += " AND n.created_at >= ?"
            params.append(since)
        if note_type:
            sql += " AND n.note_type = ?"
            params.append(note_type)
        sql += " ORDER BY n.created_at DESC LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def delete_note(self, note_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def count_notes(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM notes").fetchone()["n"])

    # ---------- 周期统计（M4 简报） ----------

    def sources_since(self, since: str, *, limit: int = 200) -> list[sqlite3.Row]:
        """区间内已消化的内容。

        字数从 segments 聚合——sources 表不存字数（避免与实际内容不一致）。
        """
        return self.conn.execute(
            "SELECT s.*, COALESCE(SUM(LENGTH(g.text)), 0) AS char_count "
            "FROM sources s LEFT JOIN segments g ON g.source_id = s.id "
            "WHERE s.created_at >= ? AND s.status = 'distilled' "
            "GROUP BY s.id ORDER BY s.created_at DESC LIMIT ?",
            (since, limit),
        ).fetchall()

    def relations_since(self, since: str, *, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT r.*, sf.title AS from_title, st.title AS to_title "
            "FROM relations r "
            "JOIN sources sf ON sf.id = r.from_source "
            "JOIN sources st ON st.id = r.to_source "
            "WHERE r.created_at >= ? "
            "AND (r.verified_by_user IS NULL OR r.verified_by_user = 1) "
            "ORDER BY r.confidence DESC LIMIT ?",
            (since, limit),
        ).fetchall()

    def pending_actions(self, *, limit: int = 30) -> list[sqlite3.Row]:
        """未处理的可执行项。

        从 digests 的 payload 里取 actions——它们是最容易被遗忘的部分，
        也是重温价值最高的内容。
        """
        return self.conn.execute(
            "SELECT d.source_id, d.payload_json, s.title, s.created_at "
            "FROM digests d JOIN sources s ON s.id = d.source_id "
            "WHERE d.payload_json LIKE '%\"actions\"%' "
            "ORDER BY s.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    # ---------- 订阅（M6） ----------

    def add_subscription(
        self,
        name: str,
        instruction: str,
        *,
        query_ir: dict | None = None,
        domain: str | None = None,
        interval_hours: int = 24,
        max_items: int = 5,
        min_density: float | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO subscriptions(name, instruction, query_ir_json, domain, "
            "interval_hours, max_items, min_density, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                name,
                instruction,
                json.dumps(query_ir or {}, ensure_ascii=False),
                domain,
                interval_hours,
                max_items,
                min_density,
                _iso(utcnow()),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_subscriptions(self, *, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM subscriptions"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        return self.conn.execute(sql).fetchall()

    def get_subscription(self, ident: str | int) -> sqlite3.Row | None:
        """按 id 或名称取订阅。CLI 里两种都该能用。"""
        if isinstance(ident, int) or str(ident).isdigit():
            row = self.conn.execute(
                "SELECT * FROM subscriptions WHERE id = ?", (int(ident),)
            ).fetchone()
            if row:
                return row
        return self.conn.execute(
            "SELECT * FROM subscriptions WHERE name = ?", (str(ident),)
        ).fetchone()

    def set_subscription_enabled(self, sub_id: int, enabled: bool) -> None:
        self.conn.execute(
            "UPDATE subscriptions SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, sub_id),
        )
        self.conn.commit()

    def delete_subscription(self, sub_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def due_subscriptions(self) -> list[sqlite3.Row]:
        """到期该跑的订阅。从未跑过的立即到期。"""
        rows = self.conn.execute(
            "SELECT * FROM subscriptions WHERE enabled = 1 ORDER BY id"
        ).fetchall()
        now = utcnow()
        due: list[sqlite3.Row] = []
        for row in rows:
            if not row["last_run_at"]:
                due.append(row)
                continue
            try:
                last = datetime.fromisoformat(row["last_run_at"].replace("Z", "+00:00"))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
            except ValueError:
                due.append(row)
                continue
            if (now - last).total_seconds() >= row["interval_hours"] * 3600:
                due.append(row)
        return due

    def start_subscription_run(self, sub_id: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO subscription_runs(sub_id, started_at) VALUES(?,?)",
            (sub_id, _iso(utcnow())),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_subscription_run(
        self,
        run_id: int,
        sub_id: int,
        *,
        status: str,
        candidates: int = 0,
        added: int = 0,
        skipped: int = 0,
        cost_usd: float = 0.0,
        note: str | None = None,
        cursor: str | None = None,
    ) -> None:
        now = _iso(utcnow())
        self.conn.execute(
            "UPDATE subscription_runs SET finished_at = ?, status = ?, candidates = ?, "
            "added = ?, skipped = ?, cost_usd = ?, note = ? WHERE id = ?",
            (now, status, candidates, added, skipped, cost_usd, note, run_id),
        )
        self.conn.execute(
            "UPDATE subscriptions SET last_run_at = ?, last_status = ?, last_error = ?, "
            "total_runs = total_runs + 1, total_added = total_added + ?, "
            "total_skipped = total_skipped + ?, total_cost_usd = total_cost_usd + ?, "
            "since_cursor = COALESCE(?, since_cursor) WHERE id = ?",
            (
                now,
                status,
                note if status == "failed" else None,
                added,
                skipped,
                cost_usd,
                cursor,
                sub_id,
            ),
        )
        self.conn.commit()

    def subscription_runs(self, sub_id: int, *, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM subscription_runs WHERE sub_id = ? "
            "ORDER BY started_at DESC LIMIT ?",
            (sub_id, limit),
        ).fetchall()

    def mark_seen(self, sub_id: int, url: str, outcome: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO subscription_seen(sub_id, url, seen_at, outcome) "
            "VALUES(?,?,?,?)",
            (sub_id, url, _iso(utcnow()), outcome),
        )
        self.conn.commit()

    def is_seen(self, sub_id: int, url: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM subscription_seen WHERE sub_id = ? AND url = ?",
                (sub_id, url),
            ).fetchone()
            is not None
        )

    def seen_count(self, sub_id: int) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM subscription_seen WHERE sub_id = ?",
                (sub_id,),
            ).fetchone()["n"]
        )

    # ---------- usage ----------

    def log_usage(
        self, *, source_id: int | None, task: str, provider: str,
        model: str, tokens_in: int, tokens_out: int, cost_usd: float,
    ) -> None:
        self.conn.execute(
            "INSERT INTO usage_log(source_id, task, provider, model, tokens_in, "
            "tokens_out, cost_usd, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (source_id, task, provider, model, tokens_in, tokens_out,
             cost_usd, _iso(utcnow())),
        )
        self.conn.commit()

    def month_cost(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM usage_log "
            "WHERE created_at >= date('now', 'start of month')"
        ).fetchone()
        return float(row["total"])

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
