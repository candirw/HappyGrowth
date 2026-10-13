-- Insight Distiller schema v2
-- M2：向量索引存储。
--
-- 设计取舍：向量直接以 float32 二进制存 SQLite BLOB，检索时用 numpy 批量计算。
-- 个人规模（数万 chunk）下毫秒级完成，且零额外依赖、跨平台无风险。
-- 规模变大时可切到 LanceDB 后端（配置 retrieval.backend=lancedb），
-- 检索接口不变，因此切换不影响上层代码。

CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id   INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    source_id  INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vector     BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_embeddings_source ON embeddings(source_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_model  ON embeddings(model);

-- chunk 全文检索：混合召回的 BM25 一路。
-- 纯向量在专有名词、数字、代码标识符上召回很差，BM25 是必需补充。
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
