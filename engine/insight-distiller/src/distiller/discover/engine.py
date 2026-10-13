"""发现流程编排。

链路：解析 → 歧义检测 → 实体消歧 → 平台检索 → 主题漏斗 → 过滤 → 排序 → 成本预演

与消化流程彻底解耦：本层只产出候选清单，交给上层决定是否消化。
这样反爬失败、平台能力不足都不会污染已经稳定的消化链路。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..config import Config
from ..llm import LLMRouter
from ..store import Database
from . import clarify
from .models import Candidate, Query
from .parser import QueryParser
from .planner import Estimate, estimate
from .providers import DiscoveryError, get_provider
from .ranker import apply_filters, rank_candidates
from .topic import TopicFilter, load_user_topics

ProgressFn = Callable[[str], None]


@dataclass
class DiscoveryPlan:
    """发现结果。候选 + 计划说明 + 成本预演。"""

    query: Query
    candidates: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    estimate: Estimate | None = None
    resolve_options: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def needs_clarify(self) -> bool:
        return self.query.needs_clarify or bool(self.resolve_options)


class DiscoveryEngine:
    def __init__(
        self,
        config: Config,
        db: Database | None = None,
        *,
        profile: str | None = None,
        progress: ProgressFn | None = None,
        use_llm: bool = True,
    ) -> None:
        self.config = config
        self.db = db
        self.progress = progress or (lambda _m: None)
        self.router: LLMRouter | None = None
        if use_llm:
            try:
                router = LLMRouter(config, profile)
                # 只有确实存在可用 provider 才启用 LLM 层
                config.task("map", profile)
                self.router = router
            except Exception:  # noqa: BLE001 - 无可用 LLM 时退化为纯规则
                self.router = None

    # ---------- 阶段一：解析（不发请求，零成本） ----------

    def plan(self, text: str) -> DiscoveryPlan:
        query = QueryParser(self.router).parse(text)
        query = clarify.detect_issues(query)
        return DiscoveryPlan(query=query)

    # ---------- 阶段二：消歧 ----------

    def resolve(self, plan: DiscoveryPlan) -> DiscoveryPlan:
        """实体消歧。命中唯一则自动确认，多个则返回选项供用户选择。"""
        query = plan.query
        if query.needs_clarify or not query.targets:
            return plan

        target = query.targets[0]
        if target.resolved_id or not target.raw:
            return plan

        from .models import TargetType

        if target.type is not TargetType.UPLOADER:
            # 关键词搜索无需消歧
            return plan

        provider = get_provider(target.platform, self.config.ingest)
        self.progress(f"查找「{target.raw}」")
        options = provider.resolve(target.raw)

        if not options:
            raise DiscoveryError(
                f"没有找到名为「{target.raw}」的 UP 主。请检查名称，"
                f"或直接提供其主页/视频链接。"
            )

        if len(options) == 1 or options[0][1] == target.raw:
            mid, name, desc = options[0]
            target.resolved_id = mid
            target.resolved_name = name
            target.confidence = 1.0 if name == target.raw else 0.8
            query.assumptions.append(f"已锁定 UP 主「{name}」（{desc}）")
            return plan

        # 多个候选：交给用户选（猜错代价高）
        plan.resolve_options = options
        return plan

    def pick_option(self, plan: DiscoveryPlan, index: int) -> DiscoveryPlan:
        if not plan.resolve_options:
            return plan
        if not (1 <= index <= len(plan.resolve_options)):
            raise DiscoveryError(f"选项 {index} 超出范围")
        mid, name, desc = plan.resolve_options[index - 1]
        target = plan.query.targets[0]
        target.resolved_id = mid
        target.resolved_name = name
        target.confidence = 1.0
        plan.query.assumptions.append(f"已选择 UP 主「{name}」（{desc}）")
        plan.resolve_options = []
        return plan

    # ---------- 阶段三：检索并出候选表 ----------

    def discover(self, plan: DiscoveryPlan) -> DiscoveryPlan:
        query = plan.query
        target = query.targets[0]
        provider = get_provider(target.platform, self.config.ingest)

        self.progress("拉取候选内容")
        candidates = provider.discover(query)
        plan.notes.append(f"平台返回 {len(candidates)} 条候选")

        # 主题漏斗
        llm_calls = 0
        if query.filters.topic:
            self.progress(f"判定主题「{'、'.join(query.filters.topic)}」")
            topic_filter = TopicFilter(
                topics=query.filters.topic,
                router=self.router,
                dictionary=load_user_topics(),
            )
            before = len(candidates)
            candidates, llm_calls = topic_filter.apply(candidates)
            plan.notes.append(
                f"主题筛选：{before} → {len(candidates)} 条"
                + (f"（{llm_calls} 次语义判定）" if llm_calls else "（全部由规则判定，零成本）")
            )

        # 过滤
        candidates, filter_notes = apply_filters(candidates, query)
        plan.notes.extend(filter_notes)

        # 排序（含平台能力补偿）
        self.progress("排序")
        result = rank_candidates(candidates, query, provider=provider)
        plan.notes.extend(result.notes)
        plan.candidates = result.candidates

        # 标记已在库中的，避免重复消化
        if self.db is not None:
            for cand in plan.candidates:
                if self.db.find_source_by_url(cand.url) is not None:
                    cand.in_library = True

        # 成本预演
        plan.estimate = estimate(
            plan.candidates, self.config, topic_llm_calls=llm_calls
        )
        return plan

    def run(self, text: str) -> DiscoveryPlan:
        """完整流程。遇到需要澄清的点会提前返回。"""
        plan = self.plan(text)
        if plan.query.needs_clarify:
            return plan
        plan = self.resolve(plan)
        if plan.needs_clarify:
            return plan
        return self.discover(plan)
