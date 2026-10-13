"""Python SDK：同进程直接调用。

如果你的理财 agent 也用 Python，这是最快路径——
不走网络、不起服务、没有序列化开销。

```python
from distiller import KnowledgeBase

with KnowledgeBase() as kb:
    res = kb.search("长期国债配置", domain="finance",
                    claim_types=["fact", "data"],   # 只要事实，排除观点
                    max_staleness_days=90,
                    token_budget=2000)
    if res.coverage.sufficiency == "empty":
        ...   # 知识库无依据，agent 应明确降级
```
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ..compat.paths import get_data_paths
from ..config import Config, load_config
from ..store import Database
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
    TimelineResponse,
)
from .service import AgentService


def _as_claim_types(values: Iterable[str | ClaimType] | None) -> list[ClaimType] | None:
    if not values:
        return None
    out: list[ClaimType] = []
    for v in values:
        out.append(v if isinstance(v, ClaimType) else ClaimType(str(v)))
    return out


class KnowledgeBase:
    """知识库的只读访问入口（ingest 除外）。

    可作为上下文管理器使用以确保连接释放；
    也可长期持有，但注意 SQLite 连接不可跨线程共享。
    """

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        config: Config | None = None,
        config_path: str | None = None,
        profile: str | None = None,
    ) -> None:
        self.config = config or load_config(config_path)
        if data_dir is not None:
            self.config.storage.data_dir = str(Path(data_dir).expanduser())
        self.profile = profile
        self.paths = get_data_paths(
            self.config.storage.data_dir,
            use_system_dir=self.config.storage.use_system_dir,
        )
        self.paths.ensure()
        self.db = Database(self.paths.db)
        self.service = AgentService(self.db, self.config)

    # ---------- 查询 ----------

    def search(
        self,
        query: str = "",
        *,
        domain: str | None = None,
        claim_types: Iterable[str | ClaimType] | None = None,
        max_staleness_days: int | None = None,
        min_authority: str | Authority | None = None,
        subject: str | None = None,
        limit: int = 20,
        token_budget: int | None = None,
        include_conflicts: bool = True,
    ) -> SearchResponse:
        """检索 claim。返回结果强制带时效、权威度与出处。"""
        return self.service.search(
            SearchRequest(
                query=query,
                domain=domain,
                claim_types=_as_claim_types(claim_types),
                max_staleness_days=max_staleness_days,
                min_authority=(
                    min_authority
                    if isinstance(min_authority, Authority) or min_authority is None
                    else Authority(str(min_authority))
                ),
                subject=subject,
                limit=limit,
                token_budget=token_budget or self.config.agent.default_token_budget,
                include_conflicts=include_conflicts,
            )
        )

    def get_claims(
        self,
        subject: str,
        *,
        domain: str | None = None,
        max_staleness_days: int | None = None,
        limit: int = 40,
    ) -> ClaimGroups:
        """取某主题全部主张，自动分组为共识/分歧/存疑。"""
        return self.service.get_claims(
            subject, domain=domain, max_staleness_days=max_staleness_days, limit=limit
        )

    def conflicts(
        self, topic: str, *, domain: str | None = None, limit: int = 20
    ) -> ConflictsResponse:
        """列出观点冲突。决策前的必查项。"""
        return self.service.conflicts(topic, domain=domain, limit=limit)

    def timeline(
        self, topic: str, *, domain: str | None = None, limit: int = 30
    ) -> TimelineResponse:
        return self.service.timeline(topic, domain=domain, limit=limit)

    def stats(self, domain: str | None = None) -> DomainStats:
        """先确认知识边界，再决定有多少底气回答。"""
        return self.service.stats(domain)

    def get_source(self, source_id: int, *, level: str = "full") -> SourceSummary | None:
        return self.service.get_source(source_id, level=level)

    def ask(
        self,
        question: str,
        *,
        domain: str | None = None,
        source_ids: list[int] | None = None,
        top_n: int | None = None,
        include_claims: bool = True,
    ) -> AskResponse:
        """基于知识库问答。会产生 LLM 调用成本。"""
        return self.service.ask(
            question,
            domain=domain,
            source_ids=source_ids,
            top_n=top_n,
            include_claims=include_claims,
            profile=self.profile,
        )

    # ---------- 写入 ----------

    def ingest(
        self,
        url: str | None = None,
        *,
        text: str | None = None,
        title: str | None = None,
        force: bool = False,
    ) -> SourceSummary | None:
        """同步消化一条内容。

        视频转写可能耗时数分钟，agent 场景建议改用 HTTP 的异步任务接口。
        """
        from ..pipeline import Pipeline

        if not url and not text:
            raise ValueError("需要提供 url 或 text")

        with Pipeline(self.config, profile=self.profile) as pipe:
            raw = pipe.fetch(url) if url else pipe.from_paste(text or "", title=title)
            result = pipe.process(raw, force=force)
            sid = result.source.id

        # Pipeline 用的是另一个连接，重新查以拿到最新状态
        return self.service.get_source(int(sid)) if sid else None

    # ---------- 生命周期 ----------

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "KnowledgeBase":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["KnowledgeBase"]
