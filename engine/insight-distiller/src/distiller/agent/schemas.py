"""Agent 接口契约（Pydantic）。

**这是 MCP 与 HTTP 两个通道的唯一数据源。**
两边都从这里生成 schema，避免契约漂移——一旦各写一份，
迟早会出现「MCP 返回了 HTTP 没有的字段」这类问题。

核心设计（见 AGENT_API.md §4）：
`staleness_days`、`authority`、`citation` 在 schema 层面设为 required，
**让 agent 不可能拿到「没有出处、没有时效、没有权威度」的裸结论**。
这比在文档里写注意事项有效得多。
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ClaimType(str, Enum):
    """区分这几类是本设计的核心。

    理财场景尤其致命：不区分事实/观点/预测，
    agent 必然把自媒体观点当客观事实用。
    """

    FACT = "fact"              # 可直接采信
    OPINION = "opinion"        # 只能作为「某人的看法」
    PREDICTION = "prediction"  # 未经验证，agent 必须标注不确定性
    METHOD = "method"          # 方法论
    DATA = "data"              # 可参与计算的数据


class Authority(str, Enum):
    OFFICIAL = "official"            # 央行、交易所、监管
    PEER_REVIEWED = "peer_reviewed"
    MEDIA = "media"
    SELF_MEDIA = "self_media"        # 自媒体，理财场景应大幅降权
    ANONYMOUS = "anonymous"


# 权威度排序，用于 min_authority 过滤
_AUTHORITY_RANK = {
    Authority.ANONYMOUS: 0,
    Authority.SELF_MEDIA: 1,
    Authority.MEDIA: 2,
    Authority.PEER_REVIEWED: 3,
    Authority.OFFICIAL: 4,
}


def authority_rank(value: str | Authority | None) -> int:
    if value is None:
        return 0
    try:
        return _AUTHORITY_RANK[Authority(value)]
    except (ValueError, KeyError):
        return 0


class Sufficiency(str, Enum):
    """知识边界。库里没相关知识时必须明确返回 empty，
    agent 才知道该说「我的知识库里没有依据」而不是硬编答案。"""

    SUFFICIENT = "sufficient"
    THIN = "thin"
    EMPTY = "empty"


class Citation(BaseModel):
    """强制可溯源。"""

    source_id: int
    title: str
    url: str
    anchor: str | None = Field(None, description="时间戳或章节锚点")
    author: str | None = None
    publish_at: str | None = None
    deeplink: str | None = Field(None, description="可直接跳转到原文位置的链接")


class ConflictRef(BaseModel):
    claim_id: int
    predicate: str
    authority: Authority
    as_of: str | None = None
    staleness_days: int | None = None
    source_title: str | None = None


class Claim(BaseModel):
    """原子知识单元。

    required 字段刻意包含 staleness_days / authority / citation，
    从 schema 层面杜绝「裸结论」。
    """

    claim_id: int
    subject: str | None = None
    predicate: str
    claim_type: ClaimType
    polarity: str | None = None
    confidence: float | None = None
    numeric: dict[str, Any] = Field(
        default_factory=dict,
        description="结构化数字，供 agent 直接计算，避免从散文二次解析",
    )
    conditions: dict[str, Any] = Field(default_factory=dict, description="成立前提")
    as_of: str | None = Field(None, description="该主张对应的时间点，非入库时间")
    expires_hint: str | None = None
    staleness_days: int = Field(
        ..., description="距今天数。金融知识会过期，agent 必须据此判断可用性"
    )
    is_stale: bool = Field(False, description="是否超出该 domain 的默认时效")
    authority: Authority
    citation: Citation
    conflicts: list[ConflictRef] = Field(
        default_factory=list, description="与库内哪些 claim 冲突"
    )
    domain: str | None = None

    model_config = {"use_enum_values": False}


class Coverage(BaseModel):
    """知识边界，防止 agent 在空库上臆断。"""

    total_matched: int = 0
    returned: int = 0
    domain_source_count: int = 0
    domain_claim_count: int = 0
    newest_as_of: str | None = None
    oldest_as_of: str | None = None
    sufficiency: Sufficiency = Sufficiency.EMPTY
    truncated_by_budget: bool = False


class SearchRequest(BaseModel):
    query: str = Field("", description="检索词；留空则按 domain 列出")
    domain: str | None = None
    claim_types: list[ClaimType] | None = Field(
        None, description="按类型过滤。建议事实与观点分两次取、分层使用"
    )
    max_staleness_days: int | None = Field(
        None, description="超过此天数的 claim 被过滤；不传则用 domain 默认值"
    )
    min_authority: Authority | None = None
    subject: str | None = None
    limit: int = Field(20, ge=1, le=200)
    token_budget: int | None = Field(
        None,
        description="按 token 预算自动裁剪并保留最高价值 claim。"
        "比让 agent 截断字符串合理得多",
    )
    include_conflicts: bool = True


class SearchResponse(BaseModel):
    claims: list[Claim]
    coverage: Coverage
    warnings: list[str] = Field(default_factory=list)
    disclaimer: str | None = Field(
        None, description="领域免责声明，如理财 domain 强制附带"
    )


class ClaimGroups(BaseModel):
    """按共识/分歧/存疑分组。

    **刻意不做观点合并** —— 把分歧原样交给 agent，
    否则冲突观点会被平均成一个看似中立、实则误导的结论。
    """

    subject: str
    consensus: list[Claim] = Field(
        default_factory=list, description="多方一致的事实与数据"
    )
    disputed: list[Claim] = Field(
        default_factory=list, description="存在冲突的主张，agent 决策前必看"
    )
    unverified: list[Claim] = Field(
        default_factory=list, description="预测与低置信度主张"
    )
    coverage: Coverage
    warnings: list[str] = Field(default_factory=list)
    disclaimer: str | None = None


class ConflictPair(BaseModel):
    subject: str | None = None
    claim_a: Claim
    claim_b: Claim
    note: str | None = None
    newer: Literal["a", "b", "unknown"] = "unknown"


class ConflictsResponse(BaseModel):
    topic: str
    pairs: list[ConflictPair] = Field(default_factory=list)
    coverage: Coverage
    warnings: list[str] = Field(default_factory=list)


class TimelinePoint(BaseModel):
    date: str | None = None
    source_id: int
    title: str
    authority: Authority
    claims: list[str] = Field(default_factory=list)
    density: float | None = None


class TimelineResponse(BaseModel):
    topic: str
    points: list[TimelinePoint] = Field(default_factory=list)
    coverage: Coverage
    warnings: list[str] = Field(default_factory=list)


class AskRequest(BaseModel):
    question: str
    domain: str | None = None
    source_ids: list[int] | None = None
    top_n: int | None = None
    include_claims: bool = Field(
        True, description="同时返回相关 claim，便于 agent 分层使用"
    )


class AskResponse(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    conflicts_detected: bool = False
    coverage: Coverage
    warnings: list[str] = Field(default_factory=list)
    disclaimer: str | None = None
    cost_usd: float = 0.0


class SourceSummary(BaseModel):
    source_id: int
    title: str
    url: str
    author: str | None = None
    platform: str
    content_type: str
    publish_at: str | None = None
    authority: Authority
    domains: list[str] = Field(default_factory=list)
    density_score: float | None = None
    density_verdict: str | None = None
    tldr: list[str] = Field(default_factory=list)
    outline: list[dict[str, Any]] = Field(default_factory=list)
    concepts: list[dict[str, str]] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    claim_count: int = 0


class DomainStats(BaseModel):
    """让 agent 知道知识边界在哪 —— 调用范式的第一步。"""

    domain: str | None = None
    source_count: int = 0
    claim_count: int = 0
    by_claim_type: dict[str, int] = Field(default_factory=dict)
    by_authority: dict[str, int] = Field(default_factory=dict)
    newest_as_of: str | None = None
    oldest_as_of: str | None = None
    avg_density: float | None = None
    stale_ratio: float = Field(
        0.0, description="超出默认时效的 claim 占比，衡量知识新鲜度"
    )
    top_subjects: list[dict[str, Any]] = Field(default_factory=list)
    conflict_count: int = 0
    sufficiency: Sufficiency = Sufficiency.EMPTY
    disclaimer: str | None = None


class IngestRequest(BaseModel):
    url: str | None = None
    text: str | None = None
    title: str | None = None
    force: bool = False


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class TaskInfo(BaseModel):
    """视频消化耗时可达数分钟，必须异步化，否则连接超时。"""

    task_id: str
    status: TaskStatus
    created_at: str
    updated_at: str
    progress: str | None = None
    source_id: int | None = None
    error: str | None = None
    result: SourceSummary | None = None


__all__ = [
    "AskRequest",
    "AskResponse",
    "Authority",
    "Citation",
    "Claim",
    "ClaimGroups",
    "ClaimType",
    "ConflictPair",
    "ConflictRef",
    "ConflictsResponse",
    "Coverage",
    "DomainStats",
    "IngestRequest",
    "SearchRequest",
    "SearchResponse",
    "SourceSummary",
    "Sufficiency",
    "TaskInfo",
    "TaskStatus",
    "TimelinePoint",
    "TimelineResponse",
    "authority_rank",
]
