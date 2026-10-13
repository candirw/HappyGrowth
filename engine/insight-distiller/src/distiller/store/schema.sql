-- Insight Distiller schema v1
--
-- 设计原则：
-- 1. M1 就把 M6 才会用到的表（claims / entities / relations / questions / reviews）
--    一次建全，避免后期数据迁移。
-- 2. 所有时间统一存 UTC ISO8601 字符串。
-- 3. 文件路径只存**相对 data/ 的相对路径**，保证整个文件夹可跨平台拷贝。
-- 4. JSON 字段用 TEXT 存，配合 schema_version 演进，新增字段不需要改表。

PRAGMA journal_mode = WAL;      -- Windows 文件锁更严格，WAL 可显著降低锁冲突
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------- 内容主表 ----------
CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    content_type  TEXT NOT NULL,              -- video | audio | article | paper
    platform      TEXT NOT NULL,              -- bilibili | wechat | web | arxiv ...
    url           TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    author        TEXT,
    publish_at    TEXT,
    duration_sec  INTEGER,
    lang          TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    density_score REAL,
    authority     TEXT NOT NULL DEFAULT 'self_media',
    domains_json  TEXT NOT NULL DEFAULT '[]', -- 领域分区，供 agent 按 domain 检索
    credibility_json TEXT,                    -- 论文专用：venue / citations / is_preprint
    audio_rel_path   TEXT,                    -- 相对路径，跨平台安全
    warnings_json TEXT NOT NULL DEFAULT '[]',
    extra_json    TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_type    ON sources(content_type);
CREATE INDEX IF NOT EXISTS idx_sources_status  ON sources(status);
CREATE INDEX IF NOT EXISTS idx_sources_density ON sources(density_score);
CREATE INDEX IF NOT EXISTS idx_sources_created ON sources(created_at);

-- ---------- 原文片段 ----------
CREATE TABLE IF NOT EXISTS segments (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    idx       INTEGER NOT NULL,
    start_ms  INTEGER,
    end_ms    INTEGER,
    section   TEXT,
    text      TEXT NOT NULL,
    UNIQUE(source_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_segments_source ON segments(source_id);

-- 全文检索：混合检索的 BM25 一路。
-- 专有名词与数字上纯向量召回很差，FTS 是必需的补充。
CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
    text,
    content='segments',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
    INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS segments_au AFTER UPDATE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
END;

-- ---------- 向量检索单元（M2 使用） ----------
CREATE TABLE IF NOT EXISTS chunks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    idx           INTEGER NOT NULL,
    text          TEXT NOT NULL,
    segment_idxs_json TEXT NOT NULL DEFAULT '[]',
    token_count   INTEGER,
    embedding_id  TEXT,
    UNIQUE(source_id, idx)
);

-- ---------- 浓缩结果 ----------
-- payload 存 JSON，schema 随 content_type 变化（视频 vs 论文），
-- 配合 schema_version + prompt_version 支持 prompt 迭代后只重跑摘要。
CREATE TABLE IF NOT EXISTS digests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id      INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    schema_version INTEGER NOT NULL DEFAULT 1,
    prompt_version TEXT NOT NULL DEFAULT 'v1',
    payload_json   TEXT NOT NULL,
    model          TEXT,
    tokens_in      INTEGER NOT NULL DEFAULT 0,
    tokens_out     INTEGER NOT NULL DEFAULT 0,
    cost_usd       REAL NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_digests_source ON digests(source_id);

-- ---------- 实体（M3.5） ----------
CREATE TABLE IF NOT EXISTS entities (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    type         TEXT,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    definition   TEXT,
    embedding_id TEXT,
    mention_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS entity_mentions (
    entity_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    source_id  INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    segment_id INTEGER,
    context    TEXT,
    PRIMARY KEY (entity_id, source_id, segment_id)
);

-- ---------- 内容间关系（M3.5） ----------
-- rel_type: cites | same_topic | contradicts | supports | extends
--           | prerequisite | answers | duplicate
CREATE TABLE IF NOT EXISTS relations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    from_source  INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    to_source    INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    rel_type     TEXT NOT NULL,
    confidence   REAL NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL DEFAULT '{}',  -- 双方原文证据，只提示不裁决
    verified_by_user INTEGER,                  -- NULL 未确认 / 1 确认 / 0 否决
    created_at   TEXT NOT NULL,
    UNIQUE(from_source, to_source, rel_type)
);
CREATE INDEX IF NOT EXISTS idx_relations_from ON relations(from_source);
CREATE INDEX IF NOT EXISTS idx_relations_to   ON relations(to_source);

-- ---------- 原子知识单元（M3.5 抽取，M6 供 agent 消费） ----------
-- claim_type 区分 fact/opinion/prediction 是刚需：
-- 不区分则 agent 必然把自媒体观点当客观事实使用。
CREATE TABLE IF NOT EXISTS claims (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    segment_idxs_json TEXT NOT NULL DEFAULT '[]',
    domain        TEXT,
    subject       TEXT,
    predicate     TEXT NOT NULL,
    claim_type    TEXT NOT NULL,               -- fact|opinion|prediction|method|data
    polarity      TEXT,
    confidence    REAL,
    as_of         TEXT,                        -- 该主张对应的时间点，非入库时间
    expires_hint  TEXT,
    numeric_json  TEXT NOT NULL DEFAULT '{}',  -- 结构化数字，供 agent 计算
    conditions_json TEXT NOT NULL DEFAULT '{}',
    authority     TEXT,
    contradicts_json TEXT NOT NULL DEFAULT '[]',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_domain ON claims(domain);
CREATE INDEX IF NOT EXISTS idx_claims_type   ON claims(claim_type);
CREATE INDEX IF NOT EXISTS idx_claims_asof   ON claims(as_of);

-- ---------- 我的疑问追踪（M4） ----------
CREATE TABLE IF NOT EXISTS questions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id         INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    text              TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'open',
    answered_by_source INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    created_at        TEXT NOT NULL
);

-- ---------- 重温调度（M4，SM-2 简化版） ----------
CREATE TABLE IF NOT EXISTS reviews (
    source_id     INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    ease          REAL NOT NULL DEFAULT 2.5,
    interval_days INTEGER NOT NULL DEFAULT 1,
    due_at        TEXT NOT NULL,
    review_count  INTEGER NOT NULL DEFAULT 0,
    last_result   TEXT,
    starred       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reviews_due ON reviews(due_at);

-- ---------- 我的批注 ----------
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id  INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    segment_id INTEGER,
    text       TEXT NOT NULL,
    note_type  TEXT NOT NULL DEFAULT 'comment',
    created_at TEXT NOT NULL
);

-- ---------- 问答历史（M2） ----------
CREATE TABLE IF NOT EXISTS messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL,
    source_ids_json TEXT NOT NULL DEFAULT '[]',
    role           TEXT NOT NULL,
    content        TEXT NOT NULL,
    citations_json TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);

-- ---------- LLM 调用成本流水 ----------
CREATE TABLE IF NOT EXISTS usage_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id  INTEGER,
    task       TEXT NOT NULL,
    provider   TEXT NOT NULL,
    model      TEXT NOT NULL,
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd   REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_log(created_at);
