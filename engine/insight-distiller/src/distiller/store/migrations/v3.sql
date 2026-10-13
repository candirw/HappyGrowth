-- v3（M6）：订阅自动化。
--
-- M1 预留了 claims/relations/entities/reviews/notes/questions，
-- 但漏了订阅——因为当时把它当成「配置」而非「数据」。
-- 实际上它需要记录 last_run / 去重游标 / 累计统计，是有状态的数据。
--
-- 全部 IF NOT EXISTS，重复执行安全。

CREATE TABLE IF NOT EXISTS subscriptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    -- 原始自然语言指令，保留它才能在解析器升级后重新解析
    instruction   TEXT NOT NULL,
    -- 解析后的 Query IR，避免每次跑都重新调 LLM
    query_ir_json TEXT NOT NULL DEFAULT '{}',
    domain        TEXT,
    -- 调度：cron 风格过于复杂，这里只支持「每 N 小时」
    interval_hours INTEGER NOT NULL DEFAULT 24,
    -- 单次最多消化几条，防止一次跑爆预算
    max_items     INTEGER NOT NULL DEFAULT 5,
    -- 密度门槛：低于此值的候选直接丢弃，不浪费浓缩成本
    min_density   REAL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    -- 只抓比这个时间更新的内容，避免重复消化历史内容
    since_cursor  TEXT,
    last_run_at   TEXT,
    last_status   TEXT,
    last_error    TEXT,
    total_runs    INTEGER NOT NULL DEFAULT 0,
    total_added   INTEGER NOT NULL DEFAULT 0,
    total_skipped INTEGER NOT NULL DEFAULT 0,
    total_cost_usd REAL NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subs_enabled ON subscriptions(enabled);

-- 每次运行的记录。用于排查「为什么这条没抓到」
CREATE TABLE IF NOT EXISTS subscription_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sub_id      INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    candidates  INTEGER NOT NULL DEFAULT 0,
    added       INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL DEFAULT 0,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_sub_runs ON subscription_runs(sub_id, started_at);

-- 已见过的候选 URL，跨运行去重。
-- 不依赖 sources 表判重，因为被密度门槛拒掉的内容不会进 sources，
-- 否则每次运行都会重新抓取并重新评估同一批水文。
CREATE TABLE IF NOT EXISTS subscription_seen (
    sub_id     INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    url        TEXT NOT NULL,
    seen_at    TEXT NOT NULL,
    outcome    TEXT,
    PRIMARY KEY (sub_id, url)
);
