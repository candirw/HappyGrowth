"""知识管理与关联（M3.5）。

把「一堆孤立摘要」变成真正的知识库：
- 实体抽取与归一 → 跨内容概念对齐
- claim 抽取 → 原子知识单元（M6 供 agent 消费）
- 三级漏斗关联 → 成本不随库规模增长
- 图谱 / 盲区 / 时间线 → 让沉淀的知识能被再次找到
"""

from .claims import CLAIM_TYPES, Claim, ClaimExtractor
from .domain import BUILTIN_DOMAINS, DomainClassifier, DomainRule
from .entity import (
    BUILTIN_ALIASES,
    Entity,
    EntityNormalizer,
    collect_entities,
    load_user_aliases,
)
from .graph import (
    Gap,
    GraphView,
    TimelineEntry,
    build_graph,
    build_timeline,
    find_gaps,
)
from .linker import (
    REL_CITES,
    REL_CONTRADICTS,
    REL_LABELS,
    LinkResult,
    Linker,
    format_findings,
)

__all__ = [
    "BUILTIN_ALIASES",
    "BUILTIN_DOMAINS",
    "CLAIM_TYPES",
    "Claim",
    "ClaimExtractor",
    "DomainClassifier",
    "DomainRule",
    "Entity",
    "EntityNormalizer",
    "Gap",
    "GraphView",
    "LinkResult",
    "Linker",
    "REL_CITES",
    "REL_CONTRADICTS",
    "REL_LABELS",
    "TimelineEntry",
    "build_graph",
    "build_timeline",
    "collect_entities",
    "find_gaps",
    "format_findings",
    "load_user_aliases",
]
