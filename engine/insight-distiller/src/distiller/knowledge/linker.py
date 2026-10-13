"""自动关联：三级漏斗。

**核心设计目标：成本不随库规模增长。**
新内容入库要与历史比对，全量跑 LLM 会失控。三级漏斗：

① 免费层：引文关系（论文有 API）+ 实体共现（SQL join）→ 命中即确定关系
② 向量层：用 TL;DR 做 embedding 检索历史库 Top20 → 本地零成本
③ LLM 层：仅对 Top5 候选做一次批量判定 → 每篇新内容约 1 次调用（¥0.003）

即使库里有 1000 篇，成本也一样——这是关键。

**关系必须分类型**：只标「相关」没有决策价值。
其中 contradicts 价值最高（认知升级多来自发现矛盾），
但只提示不裁决，必须附双方原文证据。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..llm import LLMError, LLMRouter
from ..store import Database

ProgressFn = Callable[[str], None]

# 关系类型。顺序即展示优先级。
REL_CITES = "cites"
REL_CONTRADICTS = "contradicts"
REL_ANSWERS = "answers"
REL_EXTENDS = "extends"
REL_SUPPORTS = "supports"
REL_PREREQUISITE = "prerequisite"
REL_DUPLICATE = "duplicate"
REL_SAME_TOPIC = "same_topic"

REL_LABELS = {
    REL_CITES: ("🔗", "引用了"),
    REL_CONTRADICTS: ("⚠️", "结论冲突"),
    REL_ANSWERS: ("🎯", "回答了你的疑问"),
    REL_EXTENDS: ("📈", "是其后续工作"),
    REL_SUPPORTS: ("✅", "相互印证"),
    REL_PREREQUISITE: ("📚", "前置知识"),
    REL_DUPLICATE: ("♻️", "内容重复"),
    REL_SAME_TOPIC: ("🏷", "同主题"),
}

# LLM 判定的关系类型（引文/共现类由规则确定，不需要 LLM）
_LLM_REL_TYPES = (
    REL_CONTRADICTS, REL_SUPPORTS, REL_EXTENDS,
    REL_PREREQUISITE, REL_DUPLICATE, REL_SAME_TOPIC,
)

_LINK_SYSTEM = """你判断两篇内容之间的关系类型。

规则：
1. 只依据给定的摘要判断，**不推测、不使用外部知识**。
2 . contradicts（结论冲突）要求**双方都有明确且对立的结论**，
   仅仅侧重点不同、或一方未提及，都不算冲突。判定冲突时必须给出双方的具体主张。
3. duplicate 要求内容实质相同（同一事件/同一方法的重复叙述）。
4. 关系不明确时用 same_topic，或 none 表示无实质关系。
5. confidence 要诚实：不确定就给低分。
6. 只输出 JSON，不要 markdown 包裹。"""

_LINK_TEMPLATE = """【新内容】{new_title}
核心结论：
{new_tldr}

【库中已有内容】
{candidates}

判断新内容与每篇已有内容的关系，输出 JSON：
{{
  "relations": [
    {{
      "id": 已有内容的编号,
      "rel_type": "contradicts|supports|extends|prerequisite|duplicate|same_topic|none",
      "confidence": 0.0-1.0,
      "new_claim": "新内容的相关主张（rel_type 为 contradicts 时必填）",
      "old_claim": "已有内容的相关主张（rel_type 为 contradicts 时必填）",
      "note": "一句话说明"
    }}
  ]
}}"""


@dataclass
class LinkResult:
    relations: list[dict] = field(default_factory=list)
    answered_questions: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    llm_calls: int = 0

    @property
    def has_findings(self) -> bool:
        return bool(self.relations or self.answered_questions)


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start == -1:
        return {}
    depth = 0
    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


class Linker:
    def __init__(
        self,
        db: Database,
        *,
        router: LLMRouter | None = None,
        retriever=None,
        progress: ProgressFn | None = None,
        min_confidence: float = 0.7,
        max_relations: int = 5,
    ) -> None:
        self.db = db
        self.router = router
        self.retriever = retriever
        self.progress = progress or (lambda _m: None)
        # 只展示高置信度关系，且每篇上限 5 条——否则提示会变成骚扰
        self.min_confidence = min_confidence
        self.max_relations = max_relations

    def link(self, source_id: int, *, digest=None) -> LinkResult:
        """为新入库内容建立关联。"""
        result = LinkResult()

        # ---- ① 免费层：引文关系 ----
        self._link_citations(source_id, result)

        # ---- ① 免费层：实体共现 ----
        entity_candidates = self._entity_candidates(source_id, result)

        # ---- ② 向量层：语义召回 ----
        vector_candidates = self._vector_candidates(source_id, digest, result)

        # ---- ③ LLM 层：仅对合并后的 Top N 判定 ----
        candidates = self._merge_candidates(
            entity_candidates, vector_candidates, exclude={source_id}
        )
        if candidates and self.router is not None:
            self._llm_judge(source_id, digest, candidates[:5], result)
        elif candidates:
            # 无 LLM 时只落同主题关系，不猜测具体类型
            for cid, reason, _score in candidates[:3]:
                self.db.add_relation(
                    source_id, cid, REL_SAME_TOPIC,
                    confidence=0.5,
                    evidence={"reason": reason, "method": "规则（未经语义确认）"},
                )
                result.relations.append(
                    {
                        "id": cid, "rel_type": REL_SAME_TOPIC, "confidence": 0.5,
                        "note": reason, "title": self._title(cid),
                    }
                )

        # ---- 疑问闭环：新内容能否回答历史遗留问题 ----
        self._match_questions(source_id, result)

        result.relations = result.relations[: self.max_relations]
        return result

    # ---------- ① 免费层 ----------

    def _link_citations(self, source_id: int, result: LinkResult) -> None:
        """论文引文关系：确定性、零成本、零 LLM。"""
        row = self.db.conn.execute(
            "SELECT content_type, extra_json FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        if not row or row["content_type"] != "paper":
            return

        try:
            extra = json.loads(row["extra_json"] or "{}")
        except json.JSONDecodeError:
            return

        # 用库内已有论文的 id 匹配本文的参考文献
        refs = [r for r in (extra.get("reference_ids") or []) if isinstance(r, int)]
        if not refs:
            return

        placeholders = ",".join("?" * len(refs))
        matches = self.db.conn.execute(
            f"SELECT id, title FROM sources WHERE id IN ({placeholders})",
            refs,
        ).fetchall()
        for m in matches:
            self.db.add_relation(
                source_id, int(m["id"]), REL_CITES,
                confidence=1.0,
                evidence={"method": "引文关系（确定）"},
            )
            result.relations.append(
                {
                    "id": int(m["id"]), "rel_type": REL_CITES, "confidence": 1.0,
                    "note": "本文引用了它", "title": m["title"],
                }
            )
        if matches:
            result.notes.append(f"引文关系命中 {len(matches)} 篇（零成本）")

    def _entity_candidates(
        self, source_id: int, result: LinkResult
    ) -> list[tuple[int, str, float]]:
        """实体共现：纯 SQL join，零成本。"""
        rows = self.db.shared_entity_sources(source_id, min_shared=2, limit=10)
        candidates: list[tuple[int, str, float]] = []
        for r in rows:
            shared = int(r["shared"])
            names = (r["entities"] or "").split(",")[:3]
            candidates.append(
                (
                    int(r["source_id"]),
                    f"共享 {shared} 个概念（{('、'.join(names))}）",
                    min(1.0, 0.3 + 0.15 * shared),
                )
            )
        if candidates:
            result.notes.append(f"实体共现召回 {len(candidates)} 篇（零成本）")
        return candidates

    # ---------- ② 向量层 ----------

    def _vector_candidates(
        self, source_id: int, digest, result: LinkResult
    ) -> list[tuple[int, str, float]]:
        """用 TL;DR 检索历史库。本地计算，零 API 成本。"""
        if self.retriever is None or digest is None or not digest.tldr:
            return []
        query = " ".join(digest.tldr[:3])
        if not query.strip():
            return []
        try:
            hits = self.retriever.search(query, top_n=12)
        except Exception as exc:  # noqa: BLE001 - 检索失败不应中断关联
            result.notes.append(f"语义召回不可用：{exc}")
            return []

        seen: dict[int, float] = {}
        for hit in hits:
            if hit.source_id == source_id:
                continue
            seen[hit.source_id] = max(seen.get(hit.source_id, 0.0), hit.score)
        candidates = [
            (sid, "语义相近", min(1.0, score * 20))
            for sid, score in sorted(seen.items(), key=lambda kv: -kv[1])
        ]
        if candidates:
            result.notes.append(f"语义召回 {len(candidates)} 篇（本地零成本）")
        return candidates[:10]

    @staticmethod
    def _merge_candidates(
        *candidate_lists: list[tuple[int, str, float]],
        exclude: set[int],
    ) -> list[tuple[int, str, float]]:
        merged: dict[int, tuple[str, float]] = {}
        for lst in candidate_lists:
            for cid, reason, score in lst:
                if cid in exclude:
                    continue
                if cid in merged:
                    old_reason, old_score = merged[cid]
                    # 多路命中提升可信度
                    merged[cid] = (f"{old_reason}；{reason}", max(old_score, score) + 0.1)
                else:
                    merged[cid] = (reason, score)
        return [
            (cid, reason, score)
            for cid, (reason, score) in sorted(merged.items(), key=lambda kv: -kv[1][1])
        ]

    # ---------- ③ LLM 层 ----------

    def _llm_judge(
        self,
        source_id: int,
        digest,
        candidates: list[tuple[int, str, float]],
        result: LinkResult,
    ) -> None:
        """一次批量调用判定所有候选。成本约 ¥0.003/篇。"""
        src = self.db.get_source(source_id)
        if src is None or digest is None:
            return

        blocks: list[str] = []
        for cid, reason, _score in candidates:
            cand_digest = self.db.get_latest_digest(cid)
            cand_src = self.db.get_source(cid)
            if cand_src is None or cand_digest is None:
                continue
            tldr = "\n".join(f"  - {t}" for t in (cand_digest.tldr or [])[:4])
            blocks.append(
                f"[{cid}] {cand_src.title}\n  （召回原因：{reason}）\n{tldr}"
            )

        if not blocks:
            return

        prompt = _LINK_TEMPLATE.format(
            new_title=src.title,
            new_tldr="\n".join(f"  - {t}" for t in (digest.tldr or [])[:5]),
            candidates="\n\n".join(blocks),
        )

        self.progress(f"判定与 {len(blocks)} 篇已有内容的关系")
        try:
            resp = self.router.complete(
                "map", prompt, system=_LINK_SYSTEM, json_mode=True
            )
            result.llm_calls = 1
        except LLMError as exc:
            result.notes.append(f"关系判定失败：{exc}")
            return

        data = _parse_json(resp.text)
        for item in data.get("relations") or []:
            if not isinstance(item, dict):
                continue
            try:
                cid = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            rel_type = str(item.get("rel_type") or "none").lower()
            if rel_type in ("none", "") or rel_type not in _LLM_REL_TYPES:
                continue
            try:
                confidence = float(item.get("confidence") or 0)
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence < self.min_confidence:
                continue

            evidence: dict[str, Any] = {
                "note": str(item.get("note") or ""),
                "method": "LLM 语义判定",
            }
            # 冲突必须附双方证据：只提示不裁决，让用户自己判断谁对
            if rel_type == REL_CONTRADICTS:
                new_claim = str(item.get("new_claim") or "").strip()
                old_claim = str(item.get("old_claim") or "").strip()
                if not (new_claim and old_claim):
                    # 缺证据的冲突判定不可信，降级为同主题
                    rel_type = REL_SAME_TOPIC
                else:
                    evidence["new_claim"] = new_claim
                    evidence["old_claim"] = old_claim

            self.db.add_relation(
                source_id, cid, rel_type,
                confidence=confidence, evidence=evidence,
            )
            result.relations.append(
                {
                    "id": cid,
                    "rel_type": rel_type,
                    "confidence": confidence,
                    "note": evidence.get("note", ""),
                    "title": self._title(cid),
                    "new_claim": evidence.get("new_claim"),
                    "old_claim": evidence.get("old_claim"),
                }
            )

    # ---------- 疑问闭环 ----------

    def _match_questions(self, source_id: int, result: LinkResult) -> None:
        """检查新内容能否回答历史遗留的疑问。

        这是「知识库记得你的疑问」的实现——闭环感最强的功能。
        用向量检索而非 LLM，零成本。
        """
        if self.retriever is None:
            return
        questions = self.db.open_questions(limit=20)
        if not questions:
            return

        segments = self.db.get_segments(source_id)
        if not segments:
            return
        body = " ".join(s.text for s in segments)[:4000]

        for q in questions:
            if q["source_id"] == source_id:
                continue  # 不用自己回答自己提出的问题
            try:
                hits = self.retriever.search(q["text"], top_n=3)
            except Exception:  # noqa: BLE001
                return
            for hit in hits:
                if hit.source_id != source_id:
                    continue
                # 命中且分数够高才算回答
                if hit.score < 0.02:
                    continue
                self.db.answer_question(int(q["id"]), source_id)
                result.answered_questions.append(
                    {
                        "id": int(q["id"]),
                        "text": q["text"],
                        "from_title": q["source_title"],
                    }
                )
                break

    def _title(self, source_id: int) -> str:
        src = self.db.get_source(source_id)
        return src.title if src else f"#{source_id}"


def format_findings(result: LinkResult) -> list[str]:
    """把关联结果格式化为入库时的提示行。"""
    lines: list[str] = []
    for rel in result.relations:
        icon, label = REL_LABELS.get(rel["rel_type"], ("·", rel["rel_type"]))
        line = f"{icon} {label} #{rel['id']}《{rel['title'][:36]}》"
        if rel["rel_type"] == REL_CONTRADICTS and rel.get("new_claim"):
            line += f"\n     本文：{rel['new_claim'][:60]}"
            line += f"\n     对方：{rel['old_claim'][:60]}"
        elif rel.get("note"):
            line += f" —— {rel['note'][:50]}"
        lines.append(line)

    for q in result.answered_questions:
        lines.append(
            f"🎯 可回答你在《{(q['from_title'] or '?')[:24]}》留下的疑问："
            f"{q['text'][:44]}"
        )
    return lines
