"""MCP Server。

**为什么优先走 MCP**：规范支持 `outputSchema` + `structuredContent`，
服务端声明 schema 后必须返回符合它的结构化结果，客户端也会校验——
这正好满足「agent 拿到强类型知识」的需求。

关键实现细节：官方 SDK 从**函数类型注解**自动生成 input/outputSchema。
因此只要返回类型标成 `SearchResponse` 等 Pydantic 模型，
MCP 与 HTTP 两个通道就物理上不可能漂移——它们指向同一个类。
这比手写 JSON Schema 可靠得多。

其他要点：
- 除 `kb_ingest` 外全部只读。规范要求客户端把 annotations 视为不可信，
  所以**服务端侧也真正只读**，不能只靠声明。
- 写工具仅在 `agent.allow_write: true` 时才注册，且调用时二次校验。
- `kb_ingest` 走异步任务返回句柄，避免视频转写拖垮连接。
"""

from __future__ import annotations

import json

from ..config import Config
from .schemas import (
    AskResponse,
    Authority,
    ClaimGroups,
    ClaimType,
    ConflictsResponse,
    DomainStats,
    SearchRequest,
    SearchResponse,
    SourceSummary,
    TaskInfo,
    TimelineResponse,
)


def build_server(config: Config, *, profile: str | None = None):
    """构建 MCP Server。返回 (server, 工具名清单)。"""
    try:
        from mcp.server import MCPServer
        from mcp.types import ToolAnnotations
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "MCP Server 需要官方 SDK，请先安装：\n  pip install -e '.[mcp]'"
        ) from exc

    from ..compat.paths import get_data_paths
    from ..store import Database
    from .service import AgentService
    from .tasks import TaskManager

    paths = get_data_paths(
        config.storage.data_dir, use_system_dir=config.storage.use_system_dir
    )
    paths.ensure()
    tasks = TaskManager(config, profile=profile)

    server = MCPServer(
        name="insight-distiller",
        title="Insight Distiller 知识库",
        version="0.1.0",
        instructions=(
            "这是用户的个人知识库，内容来自他消化过的视频、博客、论文。\n\n"
            "**推荐调用顺序**：\n"
            "1. 先调 kb_stats 确认该领域的覆盖情况。"
            "sufficiency=empty 时应明确告知用户「知识库中没有相关知识」，"
            "不要用你的通用知识代替。\n"
            "2. 用 kb_search 取事实（claim_types=['fact','data']）用于推理。\n"
            "3. 用 kb_list_conflicts 检查是否存在观点分歧。\n"
            "4. 需要时再单独取 opinion，并明确标注为「某人的观点」。\n\n"
            "**重要**：每条 claim 都带 claim_type、authority 与 staleness_days。"
            "opinion 与 prediction 不可当作客观事实使用；"
            "staleness_days 较大的金融类结论可能已失效。"
        ),
    )

    READ_ONLY = ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True
    )

    def _svc():
        """每次调用新建连接：MCP 请求可能来自不同线程，
        而 SQLite 连接不可跨线程共享。"""
        db = Database(paths.db)
        return db, AgentService(db, config)

    # ---------- 只读工具 ----------

    @server.tool(
        name="kb_stats",
        description=(
            "查询知识库在某领域的覆盖情况。**建议作为调用链第一步**——"
            "先确认知识边界，再决定有多少依据可用。"
            "sufficiency=empty 表示库里没有该领域知识，此时应明确告知用户。"
        ),
        annotations=READ_ONLY,
    )
    def kb_stats(domain: str | None = None) -> DomainStats:
        db, svc = _svc()
        try:
            return svc.stats(domain)
        finally:
            db.close()

    @server.tool(
        name="kb_search",
        description=(
            "检索原子知识单元（claim）。返回结果强制携带 staleness_days、"
            "authority 与 citation，不存在「没有出处的裸结论」。\n"
            "**建议事实与观点分两次取**：先用 claim_types=['fact','data'] "
            "取客观事实用于推理，再按需单独取 opinion 并标注为「某人观点」。\n"
            "claim_type 含义：fact=可采信事实；data=可计算数据；"
            "opinion=某人看法；prediction=未验证预测；method=方法论。"
        ),
        annotations=READ_ONLY,
    )
    def kb_search(
        query: str,
        domain: str | None = None,
        claim_types: list[str] | None = None,
        max_staleness_days: int | None = None,
        min_authority: str | None = None,
        limit: int = 20,
        token_budget: int | None = None,
    ) -> SearchResponse:
        db, svc = _svc()
        try:
            return svc.search(
                SearchRequest(
                    query=query,
                    domain=domain,
                    claim_types=(
                        [ClaimType(t) for t in claim_types] if claim_types else None
                    ),
                    max_staleness_days=max_staleness_days,
                    min_authority=(
                        Authority(min_authority) if min_authority else None
                    ),
                    limit=limit,
                    token_budget=token_budget or config.agent.default_token_budget,
                )
            )
        finally:
            db.close()

    @server.tool(
        name="kb_get_claims",
        description=(
            "取某主题下全部主张，**自动分组为共识/分歧/存疑**。"
            "分歧不做合并，原样返回——把冲突观点平均成中立结论会误导决策。"
        ),
        annotations=READ_ONLY,
    )
    def kb_get_claims(
        subject: str,
        domain: str | None = None,
        max_staleness_days: int | None = None,
        limit: int = 40,
    ) -> ClaimGroups:
        db, svc = _svc()
        try:
            return svc.get_claims(
                subject,
                domain=domain,
                max_staleness_days=max_staleness_days,
                limit=limit,
            )
        finally:
            db.close()

    @server.tool(
        name="kb_list_conflicts",
        description=(
            "列出某主题下的观点冲突，含双方主张、权威度与时效对比。"
            "**决策前的必查项**：库内有相反观点时应如实向用户呈现分歧。"
        ),
        annotations=READ_ONLY,
    )
    def kb_list_conflicts(
        topic: str, domain: str | None = None, limit: int = 20
    ) -> ConflictsResponse:
        db, svc = _svc()
        try:
            return svc.conflicts(topic, domain=domain, limit=limit)
        finally:
            db.close()

    @server.tool(
        name="kb_timeline",
        description="某主题的观点演进时间线，用于判断「最新共识是什么」。",
        annotations=READ_ONLY,
    )
    def kb_timeline(
        topic: str, domain: str | None = None, limit: int = 30
    ) -> TimelineResponse:
        db, svc = _svc()
        try:
            return svc.timeline(topic, domain=domain, limit=limit)
        finally:
            db.close()

    @server.tool(
        name="kb_get_source",
        description=(
            "取某篇内容的浓缩结果。level=tldr 只要核心结论，"
            "full 含分层大纲与概念表。"
        ),
        annotations=READ_ONLY,
    )
    def kb_get_source(source_id: int, level: str = "full") -> SourceSummary:
        db, svc = _svc()
        try:
            data = svc.get_source(source_id, level=level)
            if data is None:
                raise ValueError(f"找不到内容 #{source_id}")
            return data
        finally:
            db.close()

    @server.tool(
        name="kb_ask",
        description=(
            "基于知识库问答，回答带引用与出处。"
            "**会产生 LLM 调用成本**，若只需结构化事实请优先用 kb_search。"
        ),
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False),
    )
    def kb_ask(
        question: str,
        domain: str | None = None,
        source_ids: list[int] | None = None,
    ) -> AskResponse:
        db, svc = _svc()
        try:
            return svc.ask(
                question, domain=domain, source_ids=source_ids, profile=profile
            )
        finally:
            db.close()

    tool_names = [
        "kb_stats", "kb_search", "kb_get_claims", "kb_list_conflicts",
        "kb_timeline", "kb_get_source", "kb_ask",
    ]

    # ---------- 写工具：仅在显式开启时注册 ----------

    if config.agent.allow_write:

        @server.tool(
            name="kb_ingest",
            description=(
                "提交新内容消化。**异步执行**（视频转写可能数分钟），"
                "返回 task_id，用 kb_task 轮询状态。"
            ),
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False),
        )
        def kb_ingest(
            url: str | None = None,
            text: str | None = None,
            title: str | None = None,
            force: bool = False,
        ) -> TaskInfo:
            # 二次校验：不能只靠「未注册」来阻止写入
            if not config.agent.allow_write:
                raise ValueError("写入接口未开启（agent.allow_write）")
            if not url and not text:
                raise ValueError("需要提供 url 或 text")
            return tasks.submit(url=url, text=text, title=title, force=force)

        @server.tool(
            name="kb_task",
            description="查询消化任务状态。",
            annotations=READ_ONLY,
        )
        def kb_task(task_id: str) -> TaskInfo:
            info = tasks.get(task_id)
            if info is None:
                raise ValueError(f"找不到任务 {task_id}")
            return info

        tool_names += ["kb_ingest", "kb_task"]

    return server, tool_names


async def run_stdio(config: Config, *, profile: str | None = None) -> None:
    """以 stdio 方式运行（Claude Desktop 等客户端的标准接入方式）。

    注意：stdio 模式下 stdout 是协议通道，任何额外打印都会破坏协议。
    """
    server, _names = build_server(config, profile=profile)
    await server.run_stdio_async()


__all__ = ["build_server", "run_stdio"]
