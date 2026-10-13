-- v4：修复中文 BM25 完全失效。
--
-- 症状：正文里明明有「山西」，FTS MATCH "山西" 返回 0 条。
-- 即混合检索的关键词一路对中文内容**完全无效**，
-- 而这个工具主要处理中文——等于一直在用纯向量检索。
--
-- 根因：SQLite 的 unicode61 分词器把连续中文当作**一个 token**。
-- 「这一次我们去了一趟山西」整串是一个 token，查「山西」自然匹配不到。
-- 查询侧原本已做二元组切分，但**索引侧没做**，两边对不上。
--
-- 为什么不用 trigram 分词器：它要求查询至少 3 个字符，
-- 而「山西」「折叠」这类 2 字词是中文检索里最常见的形态（实测返回 0）。
--
-- 方案：索引侧也存二元组，与查询侧对齐。
-- 「去了一趟山西」→「去了 了一 一趟 趟山 山西」，查「山西」即命中。
--
-- 代价：FTS 索引体积约为原文 2 倍。个人库规模（数万 chunk）可接受，
-- 换来的是关键词召回从「完全失效」变成「可用」。

-- 外部内容表模式下触发器只能塞原文，无法在 SQL 里做切分，
-- 因此改为独立表 + Python 侧写入。
DROP TRIGGER IF EXISTS chunks_ai;
DROP TRIGGER IF EXISTS chunks_ad;
DROP TRIGGER IF EXISTS chunks_au;
DROP TABLE IF EXISTS chunks_fts;

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,               -- 存二元组切分后的文本，非原文
    tokenize='unicode61'
);

-- 标记需要重建：旧库升级后 FTS 是空的，
-- 必须跑 distill index --rebuild-fts 才能恢复关键词检索。
INSERT OR REPLACE INTO meta(key, value) VALUES('fts_needs_rebuild', '1');
