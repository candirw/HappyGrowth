"""核心数据模型。

设计要点：
- ContentType 从 M1 就区分，论文与音视频后续走不同的解析与浓缩 schema，
  避免后期改数据模型。
- Digest 的 payload 用 dict 存 JSON，配合 schema_version 演进，
  这样新增浓缩字段不需要数据库迁移。
- 所有结论型字段都带 segment/chunk 引用，保证可溯源。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    """统一用 UTC 存储，展示时再转本地时区，避免跨平台/跨时区数据错乱。"""
    return datetime.now(timezone.utc)


class ContentType(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    ARTICLE = "article"
    PAPER = "paper"


class SourceStatus(str, Enum):
    PENDING = "pending"
    FETCHED = "fetched"
    TRANSCRIBED = "transcribed"
    DISTILLED = "distilled"
    FAILED = "failed"


class Authority(str, Enum):
    """来源权威度。理财等领域会据此加权，自媒体内容大幅降权。"""

    OFFICIAL = "official"
    PEER_REVIEWED = "peer_reviewed"
    MEDIA = "media"
    SELF_MEDIA = "self_media"
    ANONYMOUS = "anonymous"


class Segment(BaseModel):
    """原文片段。音视频带毫秒时间戳，文章带章节名，论文带 section。"""

    idx: int
    text: str
    start_ms: int | None = None
    end_ms: int | None = None
    section: str | None = None

    @property
    def anchor(self) -> str:
        """人类可读锚点，用于摘要中的引用标记。"""
        if self.start_ms is None:
            return self.section or f"#{self.idx}"
        total_s = self.start_ms // 1000
        h, rem = divmod(total_s, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class RawContent(BaseModel):
    """采集层的统一产物。各平台 adapter 的差异到此为止。"""

    url: str
    content_type: ContentType
    platform: str
    title: str
    author: str | None = None
    publish_at: datetime | None = None
    duration_sec: int | None = None
    lang: str | None = None
    segments: list[Segment] = Field(default_factory=list)
    # 无字幕视频只有音频路径，等 M2 转写
    audio_rel_path: str | None = None
    # 采集过程的告警（如 OCR 降级、正文偏短），会展示给用户
    warnings: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def full_text(self) -> str:
        return "\n".join(s.text for s in self.segments)

    @property
    def char_count(self) -> int:
        return sum(len(s.text) for s in self.segments)


class Chunk(BaseModel):
    """送入 LLM 的语义块。保留 segment 引用以支持溯源。"""

    idx: int
    text: str
    segment_idxs: list[int]

    @property
    def first_segment(self) -> int:
        return self.segment_idxs[0] if self.segment_idxs else 0


class OutlineItem(BaseModel):
    title: str
    points: list[str] = Field(default_factory=list)
    anchor: str | None = None
    chunk_idxs: list[int] = Field(default_factory=list)


class Concept(BaseModel):
    term: str
    definition: str
    anchor: str | None = None


class Quote(BaseModel):
    text: str
    anchor: str | None = None


class QAPair(BaseModel):
    """一问一答。用于引申问题。"""

    question: str
    answer: str = ""


class QAContent(BaseModel):
    """问答体内容的浓缩结果。

    **为什么需要独立 schema**：通用摘要（tldr/outline/concepts）回答的是
    「这篇讲了什么」，而问答体内容（「每天一个宝藏问题」「XX 十问」
    「面试题精讲」）用户真正要的是「这个问题的答案是什么」。
    用摘要结构装问答，读者拿到的是一堆片段而不是答案。

    字段按「快速学会」的阅读顺序排列：
    先看 answer_brief 判断懂没懂 → 不懂看 answer_points →
    想深入看 follow_ups。prerequisites 告诉你缺哪块基础。
    """

    main_question: str = ""                 # 主问题（标题的规范化表述）
    answer_brief: str = ""                  # 一句话直答，这是「简略版本」的核心
    answer_points: list[str] = Field(default_factory=list)   # 要点展开，3-5 条
    prerequisites: list[str] = Field(default_factory=list)   # 前置概念
    follow_ups: list[QAPair] = Field(default_factory=list)   # 引申问题 + 简答
    pitfalls: list[str] = Field(default_factory=list)        # 易错点/常见误解


class Digest(BaseModel):
    """浓缩结果。分层输出，每层可独立查看。"""

    schema_version: int = 1
    prompt_version: str = "v1"
    tldr: list[str] = Field(default_factory=list)
    outline: list[OutlineItem] = Field(default_factory=list)
    concepts: list[Concept] = Field(default_factory=list)
    quotes: list[Quote] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    contrarian: list[str] = Field(default_factory=list)
    # 问答体内容才有。挂在 Digest 上而非新建表：
    # digests.payload_json 存的是整个 Digest 的 JSON dump，加字段零迁移。
    qa: QAContent | None = None
    density_score: float | None = None
    density_verdict: str | None = None
    model_used: str | None = None
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0


class Source(BaseModel):
    """入库后的内容记录。"""

    id: int | None = None
    content_type: ContentType
    platform: str
    url: str
    title: str
    author: str | None = None
    publish_at: datetime | None = None
    duration_sec: int | None = None
    lang: str | None = None
    status: SourceStatus = SourceStatus.PENDING
    density_score: float | None = None
    authority: Authority = Authority.SELF_MEDIA
    domains: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
