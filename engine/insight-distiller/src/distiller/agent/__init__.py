"""Agent 接口（M6）。

三种接入通道，共用同一套 Pydantic 契约（`schemas.py`）：
- **MCP Server**（首选）—— 强类型 outputSchema，客户端会校验
- **HTTP API** `/v1` —— 跨语言接入
- **Python SDK** —— 同进程调用，最快路径

核心不是传输协议，而是**知识的形态**：每条返回强制携带
时效（staleness_days）、权威度（authority）与出处（citation），
从 schema 层面杜绝「把自媒体观点当客观事实」。
"""

from .schemas import (
    AskRequest,
    AskResponse,
    Authority,
    Citation,
    Claim,
    ClaimGroups,
    ClaimType,
    ConflictPair,
    ConflictRef,
    ConflictsResponse,
    Coverage,
    DomainStats,
    IngestRequest,
    SearchRequest,
    SearchResponse,
    SourceSummary,
    Sufficiency,
    TaskInfo,
    TaskStatus,
    TimelinePoint,
    TimelineResponse,
    authority_rank,
)
from .sdk import KnowledgeBase
from .service import AgentService
from .tasks import TaskManager

__all__ = [
    "AgentService",
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
    "KnowledgeBase",
    "SearchRequest",
    "SearchResponse",
    "SourceSummary",
    "Sufficiency",
    "TaskInfo",
    "TaskManager",
    "TaskStatus",
    "TimelinePoint",
    "TimelineResponse",
    "authority_rank",
]
