"""订阅执行器（M6）。

把 M2.5 的自然语言选材固化成定期任务：
「B站 某UP主 理财方向 最近内容」→ 每天自动抓、自动浓缩、自动入库。

三个关键约束（都是为了防止自动化变成失控的烧钱机器）：

1. **跨运行去重用独立表** —— 不能靠 sources 判重。被密度门槛拒掉的
   内容不会进 sources，否则每次运行都会重新抓取并重新评估同一批水文。
2. **单次条数上限 + 预算检查** —— 自动化最怕的就是没人看着时烧光预算。
3. **失败不中断** —— 单条失败记录后继续，否则一条坏链接会让整个订阅停摆。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import Config
from ..store import Database


@dataclass
class RunResult:
    sub_id: int
    name: str
    status: str = "ok"
    candidates: int = 0
    added: int = 0
    skipped: int = 0
    cost_usd: float = 0.0
    added_items: list[dict] = field(default_factory=list)
    skipped_reasons: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    def note(self) -> str:
        if self.error:
            return self.error[:400]
        bits = [f"候选 {self.candidates}", f"入库 {self.added}"]
        if self.skipped_reasons:
            detail = "、".join(f"{k}×{v}" for k, v in self.skipped_reasons.items())
            bits.append(f"跳过 {self.skipped}（{detail}）")
        return " · ".join(bits)


class SubscriptionRunner:
    def __init__(
        self,
        config: Config,
        *,
        profile: str | None = None,
        progress=None,
    ) -> None:
        self.config = config
        self.profile = profile
        self.progress = progress or (lambda _m: None)

    def run_due(self, *, dry_run: bool = False) -> list[RunResult]:
        """跑所有到期订阅。"""
        db = None
        try:
            from ..compat.paths import get_data_paths

            paths = get_data_paths(
                self.config.storage.data_dir,
                use_system_dir=self.config.storage.use_system_dir,
            )
            paths.ensure()
            db = Database(paths.db)
            due = db.due_subscriptions()
            if not due:
                return []
            ids = [(int(r["id"]), r["name"]) for r in due]
        finally:
            if db:
                db.close()

        results: list[RunResult] = []
        for sub_id, name in ids:
            self.progress(f"运行订阅「{name}」")
            results.append(self.run_one(sub_id, dry_run=dry_run))
        return results

    def run_one(self, sub_id: int, *, dry_run: bool = False) -> RunResult:
        """跑单个订阅。每次新建 Pipeline 以获得独立连接。"""
        from ..pipeline import Pipeline

        with Pipeline(self.config, profile=self.profile, progress=self.progress) as pipe:
            db = pipe.db
            sub = db.get_subscription(sub_id)
            if sub is None:
                return RunResult(sub_id=sub_id, name="?", status="failed",
                                 error=f"找不到订阅 #{sub_id}")

            result = RunResult(sub_id=sub_id, name=sub["name"])
            run_id = None if dry_run else db.start_subscription_run(sub_id)

            try:
                # 预算护栏：自动化最怕没人看着时烧光预算
                budget = self.config.limits.monthly_budget_usd
                if budget:
                    spent = db.month_cost()
                    if spent >= budget:
                        result.status = "skipped"
                        result.error = f"本月已用 ${spent:.2f}，达到预算上限 ${budget}"
                        if run_id:
                            db.finish_subscription_run(
                                run_id, sub_id, status="skipped", note=result.error
                            )
                        return result

                candidates = self._discover(pipe, sub)
                result.candidates = len(candidates)

                fresh = []
                for cand in candidates:
                    url = cand.get("url")
                    if not url:
                        continue
                    if db.is_seen(sub_id, url):
                        result.skipped += 1
                        result.skipped_reasons["已处理过"] = (
                            result.skipped_reasons.get("已处理过", 0) + 1
                        )
                        continue
                    fresh.append(cand)

                # 条数上限：宁可下次再抓，也不要一次跑爆
                targets = fresh[: int(sub["max_items"])]
                if len(fresh) > len(targets):
                    result.skipped_reasons["超出单次上限"] = len(fresh) - len(targets)
                    result.skipped += len(fresh) - len(targets)

                if dry_run:
                    result.added_items = [
                        {"title": c.get("title"), "url": c.get("url")} for c in targets
                    ]
                    result.status = "dry_run"
                    return result

                min_density = sub["min_density"]
                for cand in targets:
                    url = cand["url"]
                    try:
                        raw = pipe.fetch(url)
                        ing = pipe.process(raw)
                        score = (
                            ing.digest.density_score
                            if ing.digest and ing.digest.density_score is not None
                            else None
                        )
                        # 密度门槛在浓缩后判定：只有浓缩过才知道密度。
                        # 低于门槛的内容仍标记 seen，避免下次重复烧钱。
                        if min_density is not None and score is not None and score < min_density:
                            result.skipped += 1
                            result.skipped_reasons["密度不足"] = (
                                result.skipped_reasons.get("密度不足", 0) + 1
                            )
                            db.mark_seen(sub_id, url, "low_density")
                            continue

                        result.added += 1
                        result.cost_usd += float(ing.digest.cost_usd or 0) if ing.digest else 0
                        result.added_items.append(
                            {
                                "source_id": ing.source.id,
                                "title": ing.source.title,
                                "url": url,
                                "density": score,
                            }
                        )
                        db.mark_seen(sub_id, url, "added")
                    except Exception as exc:  # noqa: BLE001 - 单条失败不中断整个订阅
                        result.skipped += 1
                        result.skipped_reasons["采集失败"] = (
                            result.skipped_reasons.get("采集失败", 0) + 1
                        )
                        db.mark_seen(sub_id, url, "failed")
                        self.progress(f"  {url} 处理失败：{exc}")

                cursor = datetime.now(timezone.utc).isoformat()
                if run_id:
                    db.finish_subscription_run(
                        run_id, sub_id,
                        status="ok",
                        candidates=result.candidates,
                        added=result.added,
                        skipped=result.skipped,
                        cost_usd=result.cost_usd,
                        note=result.note(),
                        cursor=cursor,
                    )
                return result

            except Exception as exc:  # noqa: BLE001
                result.status = "failed"
                result.error = str(exc)
                if run_id:
                    db.finish_subscription_run(
                        run_id, sub_id, status="failed", note=str(exc)[:400]
                    )
                return result

    def _discover(self, pipe, sub) -> list[dict]:
        """用 M2.5 的选材器找候选。

        保留原始指令而非只存解析结果——解析器升级后能重新解析，
        这是当初把 instruction 存进表里的原因。
        """
        from ..discover import DiscoveryEngine

        engine = DiscoveryEngine(
            self.config, pipe.db, profile=self.profile, progress=self.progress
        )
        plan = engine.plan(sub["instruction"])

        # 订阅场景**不能反问** —— 没人在旁边回答。
        # 有歧义就按当前解析继续，并在运行记录里留痕，
        # 让用户能事后发现指令写得不够明确。
        if plan.needs_clarify:
            issues = list(plan.query.blocking_questions or [])
            self.progress(
                f"  指令存在歧义（{len(issues) or 1} 处），按当前解析继续执行"
            )

        plan = engine.discover(plan)

        out: list[dict] = []
        for cand in plan.candidates:
            url = getattr(cand, "url", None)
            if url:
                out.append({"url": url, "title": getattr(cand, "title", None) or url})
        return out


def render_digest_markdown(results: list[RunResult], *, cost_month: float = 0.0) -> str:
    """每日简报。只列真正入库的内容，跳过的按原因汇总。"""
    lines = ["# 订阅日报", ""]
    total_added = sum(r.added for r in results)

    if not results:
        return "# 订阅日报\n\n今天没有到期的订阅。\n"

    lines.append(f"本次运行 {len(results)} 个订阅，新增 **{total_added}** 篇。")
    lines.append("")

    for r in results:
        lines.append(f"## {r.name}")
        lines.append("")
        if r.status == "failed":
            lines.append(f"运行失败：{r.error}")
            lines.append("")
            continue
        if r.status == "skipped":
            lines.append(f"已跳过：{r.error}")
            lines.append("")
            continue

        if r.added_items:
            for item in r.added_items:
                score = (
                    f" · 密度 {item['density']:.0f}"
                    if item.get("density") is not None
                    else ""
                )
                sid = item.get("source_id")
                prefix = f"#{sid} " if sid else ""
                lines.append(f"- {prefix}[{item['title']}]({item['url']}){score}")
        else:
            lines.append("本次没有新增内容。")
        if r.skipped_reasons:
            detail = "、".join(f"{k} {v} 条" for k, v in r.skipped_reasons.items())
            lines.append(f"")
            lines.append(f"*跳过：{detail}*")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"*本次花费 ${sum(r.cost_usd for r in results):.4f} · "
                 f"本月累计 ${cost_month:.4f}*")
    return "\n".join(lines)


__all__ = ["RunResult", "SubscriptionRunner", "render_digest_markdown"]
