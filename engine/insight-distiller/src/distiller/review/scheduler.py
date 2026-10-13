"""重温调度：SM-2 简化版 + 入队筛选。

**关键判断：传统 SRS（Anki）不能直接套用到知识内容。**

Anki 那套是为「记忆卡片」设计的——每张卡都该记住。但知识内容不是：
一篇水文不值得重温，一篇纯资讯看完就过期了。全部入队只会让重温
变成负担，最后必然放弃（这是所有 PKM 工具的通病）。

因此入队要筛选，优先级依据：
1. **有可执行项** —— 重温价值最高，因为还没做
2. **信息密度高** —— 值得反复看
3. **被关联多** —— 是知识网络的枢纽
4. **有未解疑问** —— 可能已经能回答了
5. 用户 star 的强制入队

间隔算法用简化 SM-2：够用且行为可预测，不引入 FSRS 那种需要
大量历史数据训练的方案（个人库数据量根本不够）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..store import Database

# 重温反馈档位。刻意只留三档——五档以上用户会纠结选哪个，
# 而对知识内容来说，"忘了/还行/记得牢"足够驱动间隔调整。
GRADE_FORGOT = "forgot"
GRADE_OK = "ok"
GRADE_EASY = "easy"
GRADES = (GRADE_FORGOT, GRADE_OK, GRADE_EASY)

GRADE_LABELS = {
    GRADE_FORGOT: "忘了/需重读",
    GRADE_OK: "还行/有印象",
    GRADE_EASY: "记得牢/可延后",
}

# 首次入队后的初始间隔（天）
_INITIAL_INTERVAL = 3
# ease 边界：低于 1.3 会让间隔几乎不增长，高于 3.0 会跳太远
_EASE_MIN = 1.3
_EASE_MAX = 3.0
# 单次间隔上限，避免算出「3 年后重温」这种无意义结果
_MAX_INTERVAL_DAYS = 365


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ReviewItem:
    source_id: int
    title: str
    content_type: str
    due_at: str
    interval_days: int
    review_count: int
    ease: float
    starred: bool = False
    density: float | None = None
    last_result: str | None = None
    # 为什么要重温这条——让用户知道不是随机推的
    reasons: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    tldr: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)

    @property
    def overdue_days(self) -> int:
        try:
            due = datetime.fromisoformat(self.due_at.replace("Z", "+00:00"))
        except ValueError:
            return 0
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        return max(0, (utcnow() - due).days)


def next_schedule(
    *, ease: float, interval_days: int, review_count: int, grade: str
) -> tuple[float, int]:
    """计算下次间隔。返回 (新 ease, 新间隔天数)。

    简化 SM-2：
    - forgot：间隔重置，ease 下调（这条确实没吸收）
    - ok：间隔 × ease
    - easy：间隔 × ease × 1.3，ease 上调
    """
    if grade == GRADE_FORGOT:
        new_ease = max(_EASE_MIN, ease - 0.2)
        # 不重置到 1 天——知识内容不像单词卡，隔天再看意义不大
        new_interval = 2
    elif grade == GRADE_EASY:
        new_ease = min(_EASE_MAX, ease + 0.15)
        base = interval_days if review_count > 0 else _INITIAL_INTERVAL
        new_interval = int(base * new_ease * 1.3)
    else:  # ok
        new_ease = ease
        base = interval_days if review_count > 0 else _INITIAL_INTERVAL
        new_interval = int(base * new_ease)

    new_interval = max(2, min(_MAX_INTERVAL_DAYS, new_interval))
    return round(new_ease, 2), new_interval


class ReviewScheduler:
    def __init__(
        self,
        db: Database,
        *,
        min_density: float = 45.0,
        require_value: bool = True,
    ) -> None:
        self.db = db
        # 密度门槛：水文不值得占用重温时间
        self.min_density = min_density
        # 是否要求「有可执行项 / 有疑问 / 被关联」才入队
        self.require_value = require_value

    # ---------- 入队 ----------

    def should_enqueue(self, source_id: int) -> tuple[bool, list[str]]:
        """判断该内容是否值得进入重温队列。返回 (是否入队, 理由)。"""
        src = self.db.get_source(source_id)
        if src is None:
            return False, []

        row = self.db.conn.execute(
            "SELECT starred FROM reviews WHERE source_id = ?", (source_id,)
        ).fetchone()
        if row and row["starred"]:
            return True, ["你标记了必看"]

        digest = self.db.get_latest_digest(source_id)
        reasons: list[str] = []

        density = src.density_score or (digest.density_score if digest else None)
        if density is not None and density < self.min_density:
            return False, [f"信息密度 {density:.0f} 偏低，不值得重温"]

        if digest:
            if digest.actions:
                reasons.append(f"有 {len(digest.actions)} 条可执行项尚未处理")
            if digest.open_questions:
                reasons.append(f"留有 {len(digest.open_questions)} 处存疑")
        if density is not None and density >= 65:
            reasons.append(f"信息密度 {density:.0f}，值得反复看")

        rel_count = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM relations "
            "WHERE (from_source = ? OR to_source = ?) "
            "AND (verified_by_user IS NULL OR verified_by_user = 1)",
            (source_id, source_id),
        ).fetchone()["n"]
        if rel_count >= 2:
            reasons.append(f"与 {rel_count} 篇内容有关联，是知识枢纽")

        if self.require_value and not reasons:
            return False, ["没有可执行项/疑问/关联，重温价值有限"]
        return True, reasons or ["信息密度达标"]

    def enqueue(
        self, source_id: int, *, force: bool = False, starred: bool = False
    ) -> tuple[bool, list[str]]:
        """加入重温队列。force 跳过价值判断。"""
        if not force:
            ok, reasons = self.should_enqueue(source_id)
            if not ok:
                return False, reasons
        else:
            reasons = ["手动加入"]

        exists = self.db.conn.execute(
            "SELECT source_id FROM reviews WHERE source_id = ?", (source_id,)
        ).fetchone()
        due = (utcnow() + timedelta(days=_INITIAL_INTERVAL)).isoformat()

        if exists:
            if starred:
                self.db.conn.execute(
                    "UPDATE reviews SET starred = 1 WHERE source_id = ?", (source_id,)
                )
                self.db.conn.commit()
            return True, reasons

        self.db.conn.execute(
            "INSERT INTO reviews(source_id, ease, interval_days, due_at, "
            "review_count, starred) VALUES(?,?,?,?,0,?)",
            (source_id, 2.5, _INITIAL_INTERVAL, due, 1 if starred else 0),
        )
        self.db.conn.commit()
        return True, reasons

    def auto_enqueue_all(self) -> tuple[int, int]:
        """为已消化但未入队的内容批量判定。返回 (入队数, 跳过数)。"""
        rows = self.db.conn.execute(
            "SELECT s.id FROM sources s WHERE s.status = 'distilled' "
            "AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.source_id = s.id)"
        ).fetchall()
        added = skipped = 0
        for r in rows:
            ok, _reasons = self.enqueue(int(r["id"]))
            if ok:
                added += 1
            else:
                skipped += 1
        return added, skipped

    def set_starred(self, source_id: int, starred: bool) -> None:
        self.enqueue(source_id, force=True, starred=starred)
        self.db.conn.execute(
            "UPDATE reviews SET starred = ? WHERE source_id = ?",
            (1 if starred else 0, source_id),
        )
        self.db.conn.commit()

    def remove(self, source_id: int) -> None:
        self.db.conn.execute("DELETE FROM reviews WHERE source_id = ?", (source_id,))
        self.db.conn.commit()

    # ---------- 取队列 ----------

    def due_items(self, *, limit: int = 10, include_future: bool = False) -> list[ReviewItem]:
        """取到期的重温项。starred 与逾期久的优先。"""
        now = utcnow().isoformat()
        sql = (
            "SELECT r.*, s.title, s.content_type, s.density_score "
            "FROM reviews r JOIN sources s ON s.id = r.source_id "
        )
        params: list = []
        if not include_future:
            sql += "WHERE r.due_at <= ? "
            params.append(now)
        sql += "ORDER BY r.starred DESC, r.due_at ASC LIMIT ?"
        params.append(limit)

        items: list[ReviewItem] = []
        for row in self.db.conn.execute(sql, params).fetchall():
            item = ReviewItem(
                source_id=int(row["source_id"]),
                title=row["title"],
                content_type=row["content_type"],
                due_at=row["due_at"],
                interval_days=int(row["interval_days"]),
                review_count=int(row["review_count"]),
                ease=float(row["ease"]),
                starred=bool(row["starred"]),
                density=row["density_score"],
                last_result=row["last_result"],
            )
            self._fill_content(item)
            items.append(item)
        return items

    def _fill_content(self, item: ReviewItem) -> None:
        """填充重温时要展示的内容。

        **只展示 TL;DR + 可执行项 + 我的批注**，不展示全文——
        重温的目的是唤起记忆并检查待办，不是重读一遍。
        """
        digest = self.db.get_latest_digest(item.source_id)
        if digest:
            item.tldr = list(digest.tldr or [])[:4]
            item.actions = list(digest.actions or [])
            item.open_questions = list(digest.open_questions or [])[:3]

        notes = self.db.get_notes(item.source_id)
        item.notes = [n["text"] for n in notes][:5]
        item.reasons = self._display_reasons(item, digest)

    def _display_reasons(self, item: ReviewItem, digest) -> list[str]:
        """已在队列的项要说明「为何值得看」，而不是重新判定是否该入队。

        直接调 should_enqueue 会产生矛盾提示：入队后若调高了密度阈值，
        会显示「密度偏低，不值得重温」——但它明明还在队列里。
        """
        if item.starred:
            return ["你标记了必看"]

        reasons: list[str] = []
        if digest:
            if digest.actions:
                reasons.append(f"有 {len(digest.actions)} 条可执行项尚未处理")
            if digest.open_questions:
                reasons.append(f"留有 {len(digest.open_questions)} 处存疑")
        if item.density is not None and item.density >= 65:
            reasons.append(f"信息密度 {item.density:.0f}，值得反复看")

        rel_count = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM relations "
            "WHERE (from_source = ? OR to_source = ?) "
            "AND (verified_by_user IS NULL OR verified_by_user = 1)",
            (item.source_id, item.source_id),
        ).fetchone()["n"]
        if rel_count >= 2:
            reasons.append(f"与 {rel_count} 篇内容有关联，是知识枢纽")

        if item.notes:
            reasons.append(f"你留了 {len(item.notes)} 条批注")

        return reasons or ["在重温队列中"]

    # ---------- 记录反馈 ----------

    def grade(self, source_id: int, grade: str) -> tuple[int, str]:
        """记录重温结果并计算下次时间。返回 (间隔天数, 下次日期)。"""
        if grade not in GRADES:
            raise ValueError(f"无效的反馈：{grade}（可选 {'/'.join(GRADES)}）")

        row = self.db.conn.execute(
            "SELECT ease, interval_days, review_count FROM reviews WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        if row is None:
            self.enqueue(source_id, force=True)
            row = self.db.conn.execute(
                "SELECT ease, interval_days, review_count FROM reviews WHERE source_id = ?",
                (source_id,),
            ).fetchone()

        new_ease, new_interval = next_schedule(
            ease=float(row["ease"]),
            interval_days=int(row["interval_days"]),
            review_count=int(row["review_count"]),
            grade=grade,
        )
        due = utcnow() + timedelta(days=new_interval)
        self.db.conn.execute(
            "UPDATE reviews SET ease = ?, interval_days = ?, due_at = ?, "
            "review_count = review_count + 1, last_result = ? WHERE source_id = ?",
            (new_ease, new_interval, due.isoformat(), grade, source_id),
        )
        self.db.conn.commit()
        return new_interval, due.date().isoformat()

    # ---------- 统计 ----------

    def stats(self) -> dict:
        now = utcnow().isoformat()
        total = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM reviews"
        ).fetchone()["n"]
        due = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM reviews WHERE due_at <= ?", (now,)
        ).fetchone()["n"]
        starred = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM reviews WHERE starred = 1"
        ).fetchone()["n"]
        reviewed = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM reviews WHERE review_count > 0"
        ).fetchone()["n"]
        return {
            "total": int(total),
            "due": int(due),
            "starred": int(starred),
            "reviewed": int(reviewed),
        }
