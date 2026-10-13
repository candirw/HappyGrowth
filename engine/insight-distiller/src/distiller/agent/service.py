"""Agent 服务层：把库内数据转成带标注的 claim。

三个设计要点（见 AGENT_API.md）：

1. **时效降权而非硬过滤** —— 老 claim 未必无用，但排序要靠后，
   且强制返回 `staleness_days` 让 agent 自己判断。
2. **token 预算裁剪由知识库做** —— agent 上下文有限，让库自己
   按价值排序后裁剪，比让 agent 截断字符串合理得多。
3. **coverage 必须诚实** —— 库里没有就返回 empty，
   宁可让 agent 说「我没有依据」，也不能让它硬编。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

from ..config import Config
from ..store import Database
from ..web.deeplink import deeplink
from .schemas import (
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
    SearchRequest,
    SearchResponse,
    SourceSummary,
    Sufficiency,
    TimelinePoint,
    TimelineResponse,
    authority_rank,
)

# 粗略 token 估算：中文约 1.5 字符/token，英文约 4 字符/token。
# 混合文本取 2.0 偏保守——宁可少给也不要爆 agent 的上下文。
_CHARS_PER_TOKEN = 2.0

# coverage 判定门槛
_THIN_THRESHOLD = 3

# 语义路的余弦相似度门槛（默认值，可由 agent.semantic_min_sim 覆盖）。
#
# 语义检索总会返回「最相似」的结果，不设门槛会让「库里根本没有的主题」
# 也返回一堆无关 claim，coverage 被判成 sufficient，agent 就基于无关证据硬答。
#
# 实测定标（bge-small-zh，中文短文本）：
#   相关查询   0.53 ~ 0.68（「国债 利率」→ LPR 公告 0.541）
#   无关查询   0.32 ~ 0.44（「加密货币挖矿」0.436、「量子计算机」0.324）
# 取 0.50 留出余量。长文本 chunk 的相似度普遍偏低，故做成可配置。
_SEMANTIC_MIN_SIM = 0.50


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.date()
        except ValueError:
            continue
    return None


def _loads(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class AgentService:
    """只读服务。除 ingest 外不修改任何数据。"""

    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config

    # ---------- domain 配置 ----------

    def domain_config(self, domain: str | None) -> dict:
        """取 domain 的 agent 相关配置。未配置的 domain 用保守默认值。"""
        domains = getattr(self.config.agent, "domains", {}) or {}
        cfg = domains.get(domain) if domain else None
        if cfg is None:
            return {
                "default_max_staleness_days": None,
                "disclaimer": None,
                "authority_weights": {},
                "require_as_of": False,
            }
        return {
            "default_max_staleness_days": getattr(cfg, "default_max_staleness_days", None),
            "disclaimer": getattr(cfg, "disclaimer_text", None)
            if getattr(cfg, "disclaimer", False)
            else None,
            "authority_weights": dict(getattr(cfg, "authority_weights", {}) or {}),
            "require_as_of": getattr(cfg, "require_as_of", False),
        }

    # ---------- claim 装配 ----------

    def _row_to_claim(
        self, row: sqlite3.Row, *, max_staleness: int | None, with_conflicts: bool = True
    ) -> Claim:
        as_of = _parse_date(row["as_of"]) or _parse_date(row["publish_at"])
        staleness = (_today() - as_of).days if as_of else 9999

        authority = row["authority"] or row["source_authority"] or "self_media"
        try:
            authority_enum = Authority(authority)
        except ValueError:
            authority_enum = Authority.SELF_MEDIA

        anchor = self._anchor_for(int(row["source_id"]), _loads(row["segment_idxs_json"], []))
        citation = Citation(
            source_id=int(row["source_id"]),
            title=row["title"],
            url=row["url"],
            anchor=anchor,
            author=row["author"] if "author" in row.keys() else None,
            publish_at=(row["publish_at"] or None),
            deeplink=deeplink(
                row["url"],
                platform=row["platform"] if "platform" in row.keys() else None,
                anchor=anchor,
            ),
        )

        try:
            ctype = ClaimType(row["claim_type"])
        except ValueError:
            ctype = ClaimType.OPINION  # 类型未知时按最保守的处理

        conflicts: list[ConflictRef] = []
        if with_conflicts:
            conflicts = self._conflicts_for(row)

        return Claim(
            claim_id=int(row["id"]),
            subject=row["subject"],
            predicate=row["predicate"],
            claim_type=ctype,
            polarity=row["polarity"],
            confidence=row["confidence"],
            numeric=_loads(row["numeric_json"], {}),
            conditions=_loads(row["conditions_json"], {}),
            as_of=as_of.isoformat() if as_of else None,
            expires_hint=row["expires_hint"],
            staleness_days=staleness,
            is_stale=bool(max_staleness and staleness > max_staleness),
            authority=authority_enum,
            citation=citation,
            conflicts=conflicts,
            domain=row["domain"],
        )

    def _anchor_for(self, source_id: int, segment_idxs: list) -> str | None:
        if not segment_idxs:
            return None
        try:
            segments = {s.idx: s for s in self.db.get_segments(source_id)}
            seg = segments.get(int(segment_idxs[0]))
            return seg.anchor if seg else None
        except Exception:  # noqa: BLE001 - 锚点缺失不该让检索失败
            return None

    def _conflicts_for(self, row: sqlite3.Row) -> list[ConflictRef]:
        ids = _loads(row["contradicts_json"], [])
        if not ids:
            return []
        out: list[ConflictRef] = []
        placeholders = ",".join("?" * len(ids))
        rows = self.db.conn.execute(
            f"SELECT c.id, c.predicate, c.authority, c.as_of, s.title, "
            f"       s.authority AS source_authority, s.publish_at "
            f"FROM claims c JOIN sources s ON s.id = c.source_id "
            f"WHERE c.id IN ({placeholders})",
            [int(i) for i in ids if str(i).isdigit()] or [-1],
        ).fetchall()
        for r in rows:
            as_of = _parse_date(r["as_of"]) or _parse_date(r["publish_at"])
            try:
                auth = Authority(r["authority"] or r["source_authority"] or "self_media")
            except ValueError:
                auth = Authority.SELF_MEDIA
            out.append(
                ConflictRef(
                    claim_id=int(r["id"]),
                    predicate=r["predicate"],
                    authority=auth,
                    as_of=as_of.isoformat() if as_of else None,
                    staleness_days=(_today() - as_of).days if as_of else None,
                    source_title=r["title"],
                )
            )
        return out

    # ---------- 检索 ----------

    def search(self, req: SearchRequest) -> SearchResponse:
        dcfg = self.domain_config(req.domain)
        max_staleness = (
            req.max_staleness_days
            if req.max_staleness_days is not None
            else dcfg["default_max_staleness_days"]
        )
        warnings: list[str] = []

        rows = self.db.search_claims(
            domain=req.domain,
            claim_types=[t.value for t in req.claim_types] if req.claim_types else None,
            subject=req.subject or (req.query or None),
            limit=max(req.limit * 4, 80),  # 多取一些，过滤与裁剪后仍够用
        )

        # 词面匹配会漏掉同义表述（查「利率」匹配不到「LPR」），
        # 因此补一路语义检索：先用向量找到相关内容，再取其 claim。
        # 检索索引不可用时静默跳过——不能因为没建索引就让 agent 拿不到数据。
        if req.query:
            rows = self._merge_semantic(rows, req)

        claims = [
            self._row_to_claim(
                r, max_staleness=max_staleness, with_conflicts=req.include_conflicts
            )
            for r in rows
        ]
        total_matched = len(claims)

        if req.min_authority is not None:
            floor = authority_rank(req.min_authority)
            before = len(claims)
            claims = [c for c in claims if authority_rank(c.authority) >= floor]
            if before != len(claims):
                warnings.append(
                    f"按权威度门槛 {req.min_authority.value} 过滤掉 {before - len(claims)} 条"
                )

        if max_staleness is not None:
            before = len(claims)
            claims = [c for c in claims if c.staleness_days <= max_staleness]
            if before != len(claims):
                warnings.append(
                    f"过滤掉 {before - len(claims)} 条超过 {max_staleness} 天的陈旧主张"
                )

        if dcfg["require_as_of"]:
            missing = [c for c in claims if c.as_of is None]
            if missing:
                warnings.append(
                    f"{len(missing)} 条主张缺少时间标注，该领域已配置为降低其可信度"
                )

        claims.sort(key=lambda c: self._value_score(c, dcfg), reverse=True)
        claims = claims[: req.limit]

        truncated = False
        if req.token_budget:
            claims, truncated = self._apply_budget(claims, req.token_budget)
            if truncated:
                warnings.append(
                    f"已按 {req.token_budget} token 预算裁剪，保留价值最高的 {len(claims)} 条"
                )

        coverage = self._coverage(
            req.domain, total_matched=total_matched, returned=len(claims), claims=claims
        )
        coverage.truncated_by_budget = truncated

        if coverage.sufficiency == Sufficiency.EMPTY:
            warnings.append(
                "知识库中没有匹配的主张。agent 应明确告知用户「知识库无相关依据」，"
                "不要基于通用知识作答。"
            )
        elif coverage.sufficiency == Sufficiency.THIN:
            warnings.append("匹配的主张较少，结论可能不充分，建议向用户说明证据有限。")

        conflicted = [c for c in claims if c.conflicts]
        if conflicted:
            warnings.append(
                f"{len(conflicted)} 条主张在库内存在相反观点，"
                "决策前请调用 conflicts 接口查看分歧。"
            )

        opinions = [c for c in claims if c.claim_type != ClaimType.FACT]
        if opinions and not req.claim_types:
            warnings.append(
                f"结果含 {len(opinions)} 条非事实类主张（观点/预测），"
                "不可当作客观事实使用。"
            )

        return SearchResponse(
            claims=claims,
            coverage=coverage,
            warnings=warnings,
            disclaimer=dcfg["disclaimer"],
        )

    def _merge_semantic(self, rows: list, req: SearchRequest) -> list:
        """补一路语义检索，合并进词面匹配结果。

        词面匹配的盲区很实际：查「利率」匹配不到「LPR」，
        查「回撤」匹配不到「最大亏损」。语义路能补上这类同义表述。

        **先检查索引是否存在再加载模型**：embedding 模型加载要十几秒，
        库里没索引时白等这个时间毫无意义（agent 调用尤其不能忍受）。
        """
        # 便宜的前置检查：没有任何索引就没必要碰模型
        try:
            indexed = self.db.count_indexed(self.config.retrieval.embedding_model)
        except Exception:  # noqa: BLE001
            return rows
        if not indexed:
            return rows

        try:
            from ..search import Retriever

            retriever = Retriever(self.db, self.config.retrieval)
            # 直接用向量路而非 search()：后者返回 RRF 融合分（约 1/60 量级），
            # 那个分数只表达排序、**不含相关性**——用它做门槛无效。
            # 这里要的是余弦相似度本身。
            scored = retriever._vector_search(req.query, None)
        except Exception:  # noqa: BLE001 - 模型缺失等情况属正常，退化为纯词面
            return rows

        # **相关性门槛**：语义检索总会返回「最相似」的内容，哪怕完全不相关。
        # 不设门槛的后果很严重——查库里根本没有的主题也会返回一堆无关 claim，
        # coverage 被判成 sufficient，agent 就基于无关证据硬答。
        # 这正是 AGENT_API.md §9 要防的失败模式。
        relevant = [
            cid
            for cid, score in scored
            if score >= getattr(self.config.agent, "semantic_min_sim", _SEMANTIC_MIN_SIM)
        ]
        if not relevant:
            return rows

        try:
            chunks = self.db.get_chunks_by_ids(relevant[:12])
        except Exception:  # noqa: BLE001
            return rows
        source_ids = {int(c["source_id"]) for c in chunks}
        if not source_ids:
            return rows

        have = {int(r["id"]) for r in rows}
        placeholders = ",".join("?" * len(source_ids))
        sql = (
            "SELECT c.*, s.title, s.url, s.author, s.platform, "
            "s.authority AS source_authority, s.publish_at "
            "FROM claims c JOIN sources s ON s.id = c.source_id "
            f"WHERE c.source_id IN ({placeholders})"
        )
        params: list = list(source_ids)
        if req.domain:
            sql += " AND c.domain = ?"
            params.append(req.domain)
        if req.claim_types:
            ph = ",".join("?" * len(req.claim_types))
            sql += f" AND c.claim_type IN ({ph})"
            params.extend(t.value for t in req.claim_types)
        sql += " ORDER BY c.confidence DESC NULLS LAST LIMIT 60"

        try:
            extra = self.db.conn.execute(sql, params).fetchall()
        except Exception:  # noqa: BLE001
            return rows

        return list(rows) + [r for r in extra if int(r["id"]) not in have]

    def _value_score(self, claim: Claim, dcfg: dict) -> float:
        """价值排序：事实优先、权威度加权、越新越好。

        这个排序决定 token 预算裁剪时保留谁，所以不能只看 confidence。
        """
        score = float(claim.confidence or 0.5)

        weights = dcfg.get("authority_weights") or {}
        score *= float(weights.get(claim.authority.value, 1.0))

        # 事实与数据可直接用于计算，价值高于观点
        score *= {
            ClaimType.FACT: 1.3,
            ClaimType.DATA: 1.25,
            ClaimType.METHOD: 1.0,
            ClaimType.OPINION: 0.75,
            ClaimType.PREDICTION: 0.6,
        }.get(claim.claim_type, 1.0)

        # 时效降权：半年内基本不降，两年前显著降权
        if claim.staleness_days < 9999:
            score *= max(0.25, 1.0 - (claim.staleness_days / 730) * 0.5)
        else:
            score *= 0.5  # 完全无时间标注

        if claim.numeric:
            score *= 1.1  # 带结构化数字的更有用

        return score

    @staticmethod
    def _estimate_tokens(claim: Claim) -> int:
        text = (claim.predicate or "") + (claim.subject or "")
        text += json.dumps(claim.numeric, ensure_ascii=False) if claim.numeric else ""
        text += claim.citation.title or ""
        # 每条 claim 的结构化字段开销，按固定值计
        return int(len(text) / _CHARS_PER_TOKEN) + 40

    def _apply_budget(
        self, claims: list[Claim], budget: int
    ) -> tuple[list[Claim], bool]:
        kept: list[Claim] = []
        used = 0
        for claim in claims:
            cost = self._estimate_tokens(claim)
            if used + cost > budget:
                return kept, True
            kept.append(claim)
            used += cost
        return kept, False

    # ---------- coverage ----------

    def _coverage(
        self,
        domain: str | None,
        *,
        total_matched: int,
        returned: int,
        claims: list[Claim],
    ) -> Coverage:
        if domain:
            src_count = int(
                self.db.conn.execute(
                    "SELECT COUNT(DISTINCT source_id) AS n FROM claims WHERE domain = ?",
                    (domain,),
                ).fetchone()["n"]
            )
        else:
            src_count = int(
                self.db.conn.execute(
                    "SELECT COUNT(DISTINCT source_id) AS n FROM claims"
                ).fetchone()["n"]
            )

        dates = sorted(c.as_of for c in claims if c.as_of)
        if returned == 0:
            sufficiency = Sufficiency.EMPTY
        elif returned < _THIN_THRESHOLD or src_count < 2:
            sufficiency = Sufficiency.THIN
        else:
            sufficiency = Sufficiency.SUFFICIENT

        return Coverage(
            total_matched=total_matched,
            returned=returned,
            domain_source_count=src_count,
            domain_claim_count=self.db.count_claims(domain=domain),
            newest_as_of=dates[-1] if dates else None,
            oldest_as_of=dates[0] if dates else None,
            sufficiency=sufficiency,
        )

    # ---------- 分组 ----------

    def get_claims(
        self,
        subject: str,
        *,
        domain: str | None = None,
        max_staleness_days: int | None = None,
        limit: int = 40,
    ) -> ClaimGroups:
        """按共识/分歧/存疑分组。

        **刻意不合并观点** —— 冲突原样交给 agent，
        否则会被平均成一个看似中立、实则误导的结论。
        """
        req = SearchRequest(
            query=subject,
            domain=domain,
            max_staleness_days=max_staleness_days,
            limit=limit,
        )
        res = self.search(req)

        consensus: list[Claim] = []
        disputed: list[Claim] = []
        unverified: list[Claim] = []

        for claim in res.claims:
            if claim.conflicts:
                disputed.append(claim)
            elif claim.claim_type in (ClaimType.PREDICTION, ClaimType.OPINION):
                unverified.append(claim)
            elif (claim.confidence or 0) < 0.5:
                unverified.append(claim)
            else:
                consensus.append(claim)

        warnings = list(res.warnings)
        if disputed:
            warnings.insert(
                0,
                f"存在 {len(disputed)} 条有分歧的主张，"
                "请如实向用户呈现分歧而非选择其中一方。",
            )

        return ClaimGroups(
            subject=subject,
            consensus=consensus,
            disputed=disputed,
            unverified=unverified,
            coverage=res.coverage,
            warnings=warnings,
            disclaimer=res.disclaimer,
        )

    # ---------- 冲突 ----------

    def conflicts(
        self, topic: str, *, domain: str | None = None, limit: int = 20
    ) -> ConflictsResponse:
        """列出某主题下的观点冲突。agent 决策前的必查项。"""
        req = SearchRequest(query=topic, domain=domain, limit=limit * 3)
        res = self.search(req)

        by_id = {c.claim_id: c for c in res.claims}
        pairs: list[ConflictPair] = []
        seen: set[tuple[int, int]] = set()

        for claim in res.claims:
            for ref in claim.conflicts:
                key = tuple(sorted((claim.claim_id, ref.claim_id)))
                if key in seen:
                    continue
                seen.add(key)

                other = by_id.get(ref.claim_id) or self._load_claim(ref.claim_id)
                if other is None:
                    continue

                newer = "unknown"
                if claim.as_of and other.as_of:
                    newer = "a" if claim.as_of > other.as_of else "b"

                pairs.append(
                    ConflictPair(
                        subject=claim.subject or other.subject,
                        claim_a=claim,
                        claim_b=other,
                        note=(
                            f"{claim.authority.value} vs {other.authority.value}；"
                            f"时效差 {abs(claim.staleness_days - other.staleness_days)} 天"
                        ),
                        newer=newer,
                    )
                )
                if len(pairs) >= limit:
                    break
            if len(pairs) >= limit:
                break

        warnings = list(res.warnings)
        if not pairs:
            warnings.append("该主题下未发现明确冲突，但这不等于结论可靠——也可能是覆盖不足。")

        return ConflictsResponse(
            topic=topic, pairs=pairs, coverage=res.coverage, warnings=warnings
        )

    def _load_claim(self, claim_id: int) -> Claim | None:
        row = self.db.conn.execute(
            "SELECT c.*, s.title, s.url, s.author, s.platform, "
            "       s.authority AS source_authority, s.publish_at "
            "FROM claims c JOIN sources s ON s.id = c.source_id WHERE c.id = ?",
            (claim_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_claim(row, max_staleness=None, with_conflicts=False)

    # ---------- 时间线 ----------

    def timeline(
        self, topic: str, *, domain: str | None = None, limit: int = 30
    ) -> TimelineResponse:
        """某主题的观点演进。用于判断「最新共识是什么」。"""
        rows = self.db.conn.execute(
            "SELECT DISTINCT s.id, s.title, s.publish_at, s.authority, s.density_score, "
            "       s.created_at "
            "FROM sources s JOIN claims c ON c.source_id = s.id "
            "WHERE (c.subject LIKE ? OR c.predicate LIKE ?) "
            + ("AND c.domain = ? " if domain else "")
            + "ORDER BY COALESCE(s.publish_at, s.created_at) ASC LIMIT ?",
            ([f"%{topic}%", f"%{topic}%"] + ([domain] if domain else []) + [limit]),
        ).fetchall()

        points: list[TimelinePoint] = []
        for row in rows:
            claim_rows = self.db.conn.execute(
                "SELECT predicate FROM claims WHERE source_id = ? "
                "AND (subject LIKE ? OR predicate LIKE ?) LIMIT 4",
                (int(row["id"]), f"%{topic}%", f"%{topic}%"),
            ).fetchall()
            try:
                auth = Authority(row["authority"] or "self_media")
            except ValueError:
                auth = Authority.SELF_MEDIA
            points.append(
                TimelinePoint(
                    date=(row["publish_at"] or row["created_at"] or "")[:10] or None,
                    source_id=int(row["id"]),
                    title=row["title"],
                    authority=auth,
                    claims=[r["predicate"] for r in claim_rows],
                    density=row["density_score"],
                )
            )

        coverage = self._coverage(
            domain, total_matched=len(points), returned=len(points), claims=[]
        )
        warnings: list[str] = []
        if len(points) < 2:
            warnings.append("时间线数据点不足，无法判断观点演进趋势。")

        return TimelineResponse(
            topic=topic, points=points, coverage=coverage, warnings=warnings
        )

    # ---------- 统计 ----------

    def stats(self, domain: str | None = None) -> DomainStats:
        """agent 调用范式的第一步：先确认知识边界。"""
        dcfg = self.domain_config(domain)
        where = "WHERE domain = ?" if domain else ""
        params = (domain,) if domain else ()

        by_type = {
            r["claim_type"]: int(r["n"])
            for r in self.db.conn.execute(
                f"SELECT claim_type, COUNT(*) AS n FROM claims {where} GROUP BY claim_type",
                params,
            ).fetchall()
        }
        by_auth = {
            (r["authority"] or "unknown"): int(r["n"])
            for r in self.db.conn.execute(
                f"SELECT authority, COUNT(*) AS n FROM claims {where} GROUP BY authority",
                params,
            ).fetchall()
        }

        agg = self.db.conn.execute(
            f"SELECT COUNT(*) AS total, COUNT(DISTINCT source_id) AS srcs, "
            f"MAX(as_of) AS newest, MIN(as_of) AS oldest FROM claims {where}",
            params,
        ).fetchone()

        subjects = [
            {"subject": r["subject"], "count": int(r["n"])}
            for r in self.db.conn.execute(
                f"SELECT subject, COUNT(*) AS n FROM claims {where} "
                f"{'AND' if domain else 'WHERE'} subject IS NOT NULL "
                f"GROUP BY subject ORDER BY n DESC LIMIT 10",
                params,
            ).fetchall()
        ]

        conflict_count = int(
            self.db.conn.execute(
                f"SELECT COUNT(*) AS n FROM claims {where} "
                f"{'AND' if domain else 'WHERE'} contradicts_json NOT IN ('[]', '')",
                params,
            ).fetchone()["n"]
        )

        total = int(agg["total"] or 0)
        max_staleness = dcfg["default_max_staleness_days"]
        stale_ratio = 0.0
        if total and max_staleness:
            cutoff = (_today() - timedelta(days=max_staleness)).isoformat()
            stale = int(
                self.db.conn.execute(
                    f"SELECT COUNT(*) AS n FROM claims {where} "
                    f"{'AND' if domain else 'WHERE'} (as_of IS NULL OR as_of < ?)",
                    (*params, cutoff),
                ).fetchone()["n"]
            )
            stale_ratio = round(stale / total, 3)

        density_rows = self.db.conn.execute(
            "SELECT AVG(density_score) AS avg FROM sources WHERE density_score IS NOT NULL"
            + (
                " AND id IN (SELECT DISTINCT source_id FROM claims WHERE domain = ?)"
                if domain
                else ""
            ),
            params,
        ).fetchone()

        if total == 0:
            sufficiency = Sufficiency.EMPTY
        elif total < 10 or int(agg["srcs"] or 0) < 3:
            sufficiency = Sufficiency.THIN
        else:
            sufficiency = Sufficiency.SUFFICIENT

        return DomainStats(
            domain=domain,
            source_count=int(agg["srcs"] or 0),
            claim_count=total,
            by_claim_type=by_type,
            by_authority=by_auth,
            newest_as_of=agg["newest"],
            oldest_as_of=agg["oldest"],
            avg_density=round(density_rows["avg"], 1) if density_rows["avg"] else None,
            stale_ratio=stale_ratio,
            top_subjects=subjects,
            conflict_count=conflict_count,
            sufficiency=sufficiency,
            disclaimer=dcfg["disclaimer"],
        )

    # ---------- 源详情 ----------

    def get_source(self, source_id: int, *, level: str = "full") -> SourceSummary | None:
        src = self.db.get_source(source_id)
        if src is None:
            return None
        digest = self.db.get_latest_digest(source_id)

        try:
            auth = Authority(
                src.authority.value if hasattr(src.authority, "value") else src.authority
            )
        except ValueError:
            auth = Authority.SELF_MEDIA

        summary = SourceSummary(
            source_id=source_id,
            title=src.title,
            url=src.url,
            author=src.author,
            platform=src.platform,
            content_type=src.content_type.value,
            publish_at=src.publish_at.date().isoformat() if src.publish_at else None,
            authority=auth,
            domains=list(src.domains or []),
            density_score=src.density_score,
            claim_count=int(
                self.db.conn.execute(
                    "SELECT COUNT(*) AS n FROM claims WHERE source_id = ?", (source_id,)
                ).fetchone()["n"]
            ),
        )
        if digest is None:
            return summary

        summary.density_verdict = digest.density_verdict
        summary.tldr = list(digest.tldr or [])
        if level == "tldr":
            return summary

        summary.outline = [
            {"title": o.title, "points": list(o.points or []), "anchor": o.anchor}
            for o in (digest.outline or [])
        ]
        summary.concepts = [
            {"term": c.term, "definition": c.definition} for c in (digest.concepts or [])
        ]
        summary.actions = list(digest.actions or [])
        summary.open_questions = list(digest.open_questions or [])
        return summary

    # ---------- 问答 ----------

    def ask(
        self,
        question: str,
        *,
        domain: str | None = None,
        source_ids: list[int] | None = None,
        top_n: int | None = None,
        include_claims: bool = True,
        profile: str | None = None,
    ) -> AskResponse:
        from ..search import ChatEngine

        dcfg = self.domain_config(domain)
        engine = ChatEngine(self.config, self.db, profile=profile)
        answer = engine.ask(question, source_ids=source_ids, top_n=top_n)

        citations = [
            Citation(
                source_id=h.source_id,
                title=h.title,
                url=h.url,
                anchor=h.anchor,
                deeplink=deeplink(h.url, anchor=h.anchor),
            )
            for h in answer.hits
        ]

        claims: list[Claim] = []
        conflicts_detected = False
        if include_claims:
            res = self.search(
                SearchRequest(query=question, domain=domain, limit=10)
            )
            claims = res.claims
            conflicts_detected = any(c.conflicts for c in claims)

        coverage = self._coverage(
            domain,
            total_matched=len(answer.hits),
            returned=len(answer.hits),
            claims=claims,
        )

        warnings: list[str] = []
        if not answer.hits:
            warnings.append("未检索到相关原文，回答可能缺乏依据。")
        if conflicts_detected:
            warnings.append("相关主张在库内存在分歧，请向用户如实呈现。")

        return AskResponse(
            answer=answer.text,
            citations=citations,
            claims=claims,
            conflicts_detected=conflicts_detected,
            coverage=coverage,
            warnings=warnings,
            disclaimer=dcfg["disclaimer"],
            cost_usd=round(getattr(answer, "cost_usd", 0.0) or 0.0, 6),
        )


__all__ = ["AgentService"]
