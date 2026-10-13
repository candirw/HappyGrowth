"""Query IR：自然语言选材的结构化中间表示。

设计要点：
- LLM 只负责「人话 → 这个结构」，不负责执行。约束输出 schema 可大幅降低幻觉。
- `assumptions` 字段是刻意设计的：凡是工具替用户做的决定都必须显式列出，
  而不是悄悄默认。这是"一切自动决定可见"原则的落地。
- `blocking_questions` 只在猜错代价高时才填（详见 clarify.py）。
"""

from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import BaseModel, Field


class Platform(str, Enum):
    BILIBILI = "bilibili"
    ZHIHU = "zhihu"
    WECHAT = "wechat"
    XHS = "xhs"
    WEB = "web"
    UNKNOWN = "unknown"


class TargetType(str, Enum):
    UPLOADER = "uploader"     # 某个博主/UP主的投稿
    COLUMN = "column"         # 专栏
    KEYWORD = "keyword"       # 全站关键词搜索
    FAVORITES = "favorites"   # 我的收藏夹
    URL = "url"               # 直接给了链接


class RankBy(str, Enum):
    PLAY = "play"
    FAVORITES = "favorites"
    LIKES = "likes"
    COMMENTS = "comments"
    DATE = "date"
    RELEVANCE = "relevance"


class Target(BaseModel):
    """一个检索目标。"""

    platform: Platform = Platform.UNKNOWN
    type: TargetType = TargetType.KEYWORD
    raw: str = ""                      # 用户原话，用于消歧展示
    resolved_id: str | None = None      # 消歧后的平台 ID（如 UP 主 mid）
    resolved_name: str | None = None
    confidence: float = 0.0


class DateRange(BaseModel):
    from_date: date | None = None
    to_date: date | None = None


class Duration(BaseModel):
    min_sec: int | None = None
    max_sec: int | None = None


class Filters(BaseModel):
    topic: list[str] = Field(default_factory=list)          # 语义方向，如 ["摄影"]
    date_range: DateRange = Field(default_factory=DateRange)
    duration: Duration = Field(default_factory=Duration)
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)


class Rank(BaseModel):
    by: RankBy = RankBy.PLAY
    desc: bool = True


class Question(BaseModel):
    """需要用户澄清的问题。结构化选项，避免开放式来回。"""

    key: str
    text: str
    options: list[str] = Field(default_factory=list)


class Query(BaseModel):
    """自然语言指令的结构化表示。"""

    intent: str = "discover"           # discover | single | subscribe
    targets: list[Target] = Field(default_factory=list)
    filters: Filters = Field(default_factory=Filters)
    rank: Rank = Field(default_factory=Rank)
    limit: int = 20
    # 工具自行做出的决定，必须展示给用户
    assumptions: list[str] = Field(default_factory=list)
    # 猜错代价高、必须问清的问题
    blocking_questions: list[Question] = Field(default_factory=list)
    raw_input: str = ""

    @property
    def needs_clarify(self) -> bool:
        return bool(self.blocking_questions)

    def describe(self) -> str:
        """人类可读的计划说明，用于 `distill plan` 与执行前确认。"""
        lines: list[str] = []
        for t in self.targets:
            name = t.resolved_name or t.raw or "（未指定）"
            lines.append(f"目标：{t.platform.value} · {t.type.value} · {name}")
        if self.filters.topic:
            lines.append(f"主题方向：{'、'.join(self.filters.topic)}")
        dr = self.filters.date_range
        if dr.from_date or dr.to_date:
            lines.append(
                f"时间范围：{dr.from_date or '不限'} ~ {dr.to_date or '至今'}"
            )
        d = self.filters.duration
        if d.min_sec or d.max_sec:
            lines.append(f"时长：{d.min_sec or 0}s ~ {d.max_sec or '不限'}")
        if self.filters.exclude_keywords:
            lines.append(f"排除：{'、'.join(self.filters.exclude_keywords)}")
        order = "降序" if self.rank.desc else "升序"
        lines.append(f"排序：{self.rank.by.value} {order} · 取前 {self.limit}")
        return "\n".join(lines)


class Candidate(BaseModel):
    """发现阶段产出的候选内容。尚未消化。"""

    url: str
    title: str
    platform: str
    author: str | None = None
    published: date | None = None
    duration_sec: int | None = None
    play_count: int | None = None
    favorite_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    has_subtitle: bool | None = None       # 影响成本预估（有字幕可跳过 ASR）
    topic_score: float | None = None       # 主题匹配置信度
    topic_reason: str | None = None
    in_library: bool = False               # 已消化过，避免重复花钱
    excluded_reason: str | None = None

    @property
    def metric(self) -> int:
        """当前排序指标的值，缺失时为 0。"""
        return self.play_count or 0
