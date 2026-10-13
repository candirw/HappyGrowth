"""重温与沉淀（M4）。

让沉淀的知识主动回来找你：
- 间隔重复调度（只调度值得重温的内容，不是全部）
- 周期简报（新关联/待办/盲区/疑问闭环，零 LLM 成本）
- Obsidian 双链导出（关系与概念转 wikilink，可脱离本工具存在）
- Q&A 合集导出（问答体系列内容按「问题→要点→出处」汇成速查文档）
"""

from .brief import ActionItem, Brief, build_brief, render_brief_markdown
from .qa import (
    QACollection,
    QAItem,
    build_collection,
    export_qa,
    parse_question,
    render_markdown as render_qa_markdown,
)
from .scheduler import (
    GRADE_EASY,
    GRADE_FORGOT,
    GRADE_OK,
    GRADE_LABELS,
    GRADES,
    ReviewItem,
    ReviewScheduler,
    next_schedule,
)
from .vault import VaultExporter, VaultStats, entity_page_name, wikilink_title

__all__ = [
    "ActionItem",
    "Brief",
    "GRADES",
    "GRADE_EASY",
    "GRADE_FORGOT",
    "GRADE_LABELS",
    "GRADE_OK",
    "QACollection",
    "QAItem",
    "ReviewItem",
    "ReviewScheduler",
    "VaultExporter",
    "VaultStats",
    "build_brief",
    "build_collection",
    "entity_page_name",
    "export_qa",
    "next_schedule",
    "parse_question",
    "render_brief_markdown",
    "render_qa_markdown",
    "wikilink_title",
]
