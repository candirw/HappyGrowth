"""学术 API 响应缓存。

**为什么是刚需（实测依据）**：
- OpenAlex 已改付费额度制，匿名 list 请求直接 429；
- S2 匿名共享池频繁 429，需退避重试。

论文的引文关系变化极慢（月级），因此 30 天缓存收益极高，
且能让重复分析同一篇论文时完全不发请求。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

DEFAULT_TTL_DAYS = 30


class ApiCache:
    """基于 SQLite 的简单 KV 缓存。与主库分离，可随时删除重建。"""

    def __init__(self, path: Path, *, ttl_days: int = DEFAULT_TTL_DAYS) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_days * 86400
        self.conn = sqlite3.connect(self.path, timeout=20.0)
        self.conn.execute("PRAGMA busy_timeout = 20000")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS api_cache ("
            "  key TEXT PRIMARY KEY,"
            "  payload TEXT NOT NULL,"
            "  fetched_at REAL NOT NULL"
            ")"
        )
        self.conn.commit()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        row = self.conn.execute(
            "SELECT payload, fetched_at FROM api_cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            self.misses += 1
            return None
        if time.time() - row[1] > self.ttl:
            self.misses += 1
            return None
        self.hits += 1
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return None

    def set(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO api_cache(key, payload, fetched_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, "
            "fetched_at=excluded.fetched_at",
            (key, json.dumps(value, ensure_ascii=False), time.time()),
        )
        self.conn.commit()

    def stats(self) -> str:
        total = self.hits + self.misses
        rate = f"{self.hits / total * 100:.0f}%" if total else "-"
        return f"缓存命中 {self.hits}/{total}（{rate}）"

    def close(self) -> None:
        self.conn.close()
