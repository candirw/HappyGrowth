"""HTTP API `/v1`。

与 MCP 共用 `AgentService` 与 Pydantic 契约，两个通道不会漂移。

安全默认值（见 AGENT_API.md §9）：
- 默认仅绑 `127.0.0.1`
- 读写 token 分离 —— 写接口风险远高于读
- 写接口默认关闭，需显式 `allow_write: true`
- 绑定非本机地址但未设 token 时**拒绝启动**，而不是默默暴露知识库
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..compat.paths import get_data_paths
from ..config import Config
from ..store import Database
from .schemas import (
    AskRequest,
    AskResponse,
    Authority,
    ClaimGroups,
    ClaimType,
    ConflictsResponse,
    DomainStats,
    IngestRequest,
    SearchRequest,
    SearchResponse,
    SourceSummary,
    TaskInfo,
    TimelineResponse,
)
from .service import AgentService
from .tasks import TaskManager

_bearer = HTTPBearer(auto_error=False)


class AgentDeps:
    def __init__(self, config: Config, *, profile: str | None = None) -> None:
        self.config = config
        self.profile = profile
        self.paths = get_data_paths(
            config.storage.data_dir, use_system_dir=config.storage.use_system_dir
        )
        self.paths.ensure()
        self.tasks = TaskManager(config, profile=profile)

    def service(self) -> tuple[Database, AgentService]:
        db = Database(self.paths.db)
        return db, AgentService(db, self.config)


def _deps(request: Request) -> AgentDeps:
    return request.app.state.agent_deps


def _check_token(
    request: Request,
    creds: HTTPAuthorizationCredentials | None,
    *,
    write: bool,
) -> None:
    cfg = _deps(request).config.agent
    expected = cfg.write_token if write else (cfg.read_token or cfg.write_token)

    if write and not cfg.allow_write:
        raise HTTPException(
            403,
            "写接口未开启。如需允许，请在 config.yaml 设置 agent.allow_write: true",
        )
    if not expected:
        # 未配置 token 时只允许本机访问
        client = (request.client.host if request.client else "") or ""
        if client not in ("127.0.0.1", "::1", "localhost", "testclient"):
            raise HTTPException(
                401, "未配置访问令牌时仅允许本机访问。请设置 agent.read_token。"
            )
        return
    if creds is None or creds.credentials != expected:
        raise HTTPException(401, "访问令牌无效")


def require_read(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> None:
    _check_token(request, creds, write=False)


def require_write(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> None:
    _check_token(request, creds, write=True)


router = APIRouter(prefix="/v1", tags=["agent"])


@router.get("/stats", response_model=DomainStats, dependencies=[Depends(require_read)])
def stats(request: Request, domain: str | None = Query(None)) -> DomainStats:
    """先确认知识边界。建议作为调用链第一步。"""
    db, svc = _deps(request).service()
    try:
        return svc.stats(domain)
    finally:
        db.close()


@router.get(
    "/claims/search", response_model=SearchResponse, dependencies=[Depends(require_read)]
)
def search_claims(
    request: Request,
    q: str = Query("", description="检索词"),
    domain: str | None = Query(None),
    claim_types: list[ClaimType] | None = Query(None),
    max_staleness_days: int | None = Query(None),
    min_authority: Authority | None = Query(None),
    limit: int = Query(20, ge=1, le=200),
    token_budget: int | None = Query(None),
) -> SearchResponse:
    deps = _deps(request)
    db, svc = deps.service()
    try:
        return svc.search(
            SearchRequest(
                query=q,
                domain=domain,
                claim_types=claim_types,
                max_staleness_days=max_staleness_days,
                min_authority=min_authority,
                limit=limit,
                token_budget=token_budget or deps.config.agent.default_token_budget,
            )
        )
    finally:
        db.close()


@router.get("/claims/{claim_id}", dependencies=[Depends(require_read)])
def get_claim(request: Request, claim_id: int) -> dict[str, Any]:
    db, svc = _deps(request).service()
    try:
        claim = svc._load_claim(claim_id)
        if claim is None:
            raise HTTPException(404, f"找不到 claim #{claim_id}")
        return claim.model_dump(mode="json")
    finally:
        db.close()


@router.get(
    "/topics/{topic}/claims",
    response_model=ClaimGroups,
    dependencies=[Depends(require_read)],
)
def topic_claims(
    request: Request,
    topic: str,
    domain: str | None = Query(None),
    max_staleness_days: int | None = Query(None),
    limit: int = Query(40, ge=1, le=200),
) -> ClaimGroups:
    """自动分组为共识/分歧/存疑。分歧不做合并。"""
    db, svc = _deps(request).service()
    try:
        return svc.get_claims(
            topic, domain=domain, max_staleness_days=max_staleness_days, limit=limit
        )
    finally:
        db.close()


@router.get(
    "/topics/{topic}/conflicts",
    response_model=ConflictsResponse,
    dependencies=[Depends(require_read)],
)
def topic_conflicts(
    request: Request,
    topic: str,
    domain: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
) -> ConflictsResponse:
    """决策前的必查项。"""
    db, svc = _deps(request).service()
    try:
        return svc.conflicts(topic, domain=domain, limit=limit)
    finally:
        db.close()


@router.get(
    "/topics/{topic}/timeline",
    response_model=TimelineResponse,
    dependencies=[Depends(require_read)],
)
def topic_timeline(
    request: Request,
    topic: str,
    domain: str | None = Query(None),
    limit: int = Query(30, ge=1, le=200),
) -> TimelineResponse:
    db, svc = _deps(request).service()
    try:
        return svc.timeline(topic, domain=domain, limit=limit)
    finally:
        db.close()


@router.get(
    "/sources/{source_id}",
    response_model=SourceSummary,
    dependencies=[Depends(require_read)],
)
def get_source(
    request: Request, source_id: int, level: str = Query("full", pattern="^(tldr|full)$")
) -> SourceSummary:
    db, svc = _deps(request).service()
    try:
        data = svc.get_source(source_id, level=level)
        if data is None:
            raise HTTPException(404, f"找不到内容 #{source_id}")
        return data
    finally:
        db.close()


@router.post("/ask", response_model=AskResponse, dependencies=[Depends(require_read)])
def ask(request: Request, body: AskRequest) -> AskResponse:
    """会产生 LLM 成本。只需结构化事实时请用 /v1/claims/search。"""
    deps = _deps(request)
    db, svc = deps.service()
    try:
        return svc.ask(
            body.question,
            domain=body.domain,
            source_ids=body.source_ids,
            top_n=body.top_n,
            include_claims=body.include_claims,
            profile=deps.profile,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"问答失败：{exc}") from exc
    finally:
        db.close()


@router.post("/ingest", response_model=TaskInfo, dependencies=[Depends(require_write)])
def ingest(request: Request, body: IngestRequest) -> TaskInfo:
    """异步消化。视频转写可能数分钟，返回 task_id 轮询。"""
    if not body.url and not body.text:
        raise HTTPException(400, "需要提供 url 或 text")
    return _deps(request).tasks.submit(
        url=body.url, text=body.text, title=body.title, force=body.force
    )


@router.get("/tasks/{task_id}", response_model=TaskInfo, dependencies=[Depends(require_read)])
def get_task(request: Request, task_id: str) -> TaskInfo:
    info = _deps(request).tasks.get(task_id)
    if info is None:
        raise HTTPException(404, f"找不到任务 {task_id}")
    return info


@router.get("/tasks", dependencies=[Depends(require_read)])
def list_tasks(request: Request, limit: int = Query(50, le=200)) -> dict[str, Any]:
    return {
        "tasks": [t.model_dump(mode="json") for t in _deps(request).tasks.list(limit)]
    }


def create_agent_app(config: Config, *, profile: str | None = None) -> FastAPI:
    """独立的 agent API 应用。"""
    app = FastAPI(
        title="Insight Distiller · Agent API",
        description=(
            "把知识库以带标注的原子 claim 形态开放给外部 agent。"
            "每条返回强制携带时效、权威度与出处。"
        ),
        version="1.0.0",
        docs_url="/v1/docs",
        openapi_url="/v1/openapi.json",
    )
    app.state.agent_deps = AgentDeps(config, profile=profile)
    app.include_router(router)
    return app


__all__ = ["create_agent_app", "router", "AgentDeps"]
