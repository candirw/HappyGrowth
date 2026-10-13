"""map-reduce 分层浓缩。

为什么用 map-reduce 而不是一次性丢给长上下文模型：
1. 长内容一次性输入会丢中段信息（lost in the middle）；
2. 分块可以让 map 阶段走本地免费模型，只有 reduce 用 API，成本降一个数量级；
3. 每条结论可回溯到具体 chunk，满足可溯源要求。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from ..config import Config
from ..llm import LLMError, LLMRouter
from ..models import (
    Chunk,
    Concept,
    Digest,
    OutlineItem,
    QAContent,
    QAPair,
    Quote,
    Segment,
)
from .chunker import chunk_segments
from .density import score_density
from .prompts import (
    MAP_SYSTEM,
    MAP_USER_TEMPLATE,
    PROMPT_VERSION,
    QA_SYSTEM,
    QA_USER_TEMPLATE,
    REDUCE_SYSTEM,
    REDUCE_USER_TEMPLATE,
    detect_suspicious_instructions,
    wrap_untrusted,
)

ProgressFn = Callable[[str], None]


def _parse_json(text: str) -> dict[str, Any]:
    """稳健解析 LLM 返回的 JSON。

    即使要求了 json_mode，仍可能出现 markdown 包裹或前后缀文字，
    因此必须容错——否则整条流程会因为一个多余的反引号失败。
    """
    text = (text or "").strip()
    if not text:
        return {}

    # 去掉 markdown 代码块包裹
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 提取第一个完整的 JSON 对象
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


def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if value in (None, "", {}):
        return []
    return [value]


class Summarizer:
    def __init__(
        self,
        config: Config,
        router: LLMRouter,
        *,
        progress: ProgressFn | None = None,
    ) -> None:
        self.config = config
        self.router = router
        self.progress = progress or (lambda _msg: None)

    def run(self, title: str, segments: list[Segment]) -> Digest:
        full_text = "\n".join(s.text for s in segments)
        density = score_density(full_text)

        chunks = chunk_segments(
            segments,
            chunk_size=self.config.distill.chunk_size,
            overlap=self.config.distill.chunk_overlap,
        )
        if not chunks:
            raise LLMError("没有可浓缩的内容")

        warnings: list[str] = []
        if detect_suspicious_instructions(full_text):
            warnings.append(
                "原文包含疑似指令性文本（prompt 注入），已按纯数据处理"
            )

        # 密度过低时跳过精细提取，只做一次汇总，避免在水文上烧钱
        if density.score < self.config.distill.skip_detail_below:
            self.progress(
                f"密度评分 {density.score}（{density.verdict}），跳过精读直接汇总"
            )
            block_results = [
                {"chunk_idx": c.idx, "points": [c.text[:500]]} for c in chunks[:3]
            ]
        else:
            block_results = self._map(chunks)

        digest = self._reduce(title, block_results, segments)
        digest.density_score = density.score
        digest.density_verdict = density.verdict
        digest.prompt_version = PROMPT_VERSION
        if warnings:
            digest.open_questions = warnings + digest.open_questions
        return digest

    # ---------- 问答体（单次调用） ----------

    def run_qa(self, title: str, segments: list[Segment]) -> Digest:
        """问答体内容的浓缩：标题即问题，产出答案卡片。

        **不走 map-reduce**：这类内容单期通常 2~5 分钟（1000 字左右），
        一次调用就能装下全文。分块会把推导过程切断，反而降低答案质量，
        而且把成本翻几倍。

        产出同时填 qa 与 tldr：
        - qa 给 `qa-export` 用（结构化问答）
        - tldr 给 `list` / `show` / 检索用（保持与其他内容一致）
        """
        full_text = "\n".join(s.text for s in segments)
        if not full_text.strip():
            raise LLMError("没有可浓缩的内容")

        density = score_density(full_text)
        warnings: list[str] = []
        if detect_suspicious_instructions(full_text):
            warnings.append("原文包含疑似指令性文本（prompt 注入），已按纯数据处理")

        # 超长的问答体（罕见）截断而非分块：宁可丢尾部也要保住推导完整性
        limit = max(2000, self.config.distill.chunk_size * 2)
        body = full_text[:limit]
        if len(full_text) > limit:
            warnings.append(f"原文 {len(full_text)} 字，已截取前 {limit} 字浓缩")

        # 用独立任务名 qa_card 而非 reduce：
        # reduce 默认配的是 reasoner（贵），而问答体单期只 1000 字左右，
        # chat 模型完全够用。211 期批量跑时这个差别是几倍成本。
        prompt = QA_USER_TEMPLATE.format(title=title, **wrap_untrusted(body))
        resp = self.router.complete("qa_card", prompt, system=QA_SYSTEM, json_mode=True)
        data = _parse_json(resp.text)

        qa = QAContent(
            main_question=str(data.get("main_question") or title).strip(),
            answer_brief=str(data.get("answer_brief") or "").strip(),
            answer_points=[str(p).strip() for p in _as_list(data.get("answer_points")) if str(p).strip()],
            prerequisites=[str(p).strip() for p in _as_list(data.get("prerequisites")) if str(p).strip()],
            pitfalls=[str(p).strip() for p in _as_list(data.get("pitfalls")) if str(p).strip()],
            follow_ups=[
                QAPair(
                    question=str(f.get("question") or "").strip(),
                    answer=str(f.get("answer") or "").strip(),
                )
                for f in _as_list(data.get("follow_ups"))
                if isinstance(f, dict) and str(f.get("question") or "").strip()
            ],
        )

        # tldr 用「直答 + 要点」，让 list/show/检索 与其他内容行为一致
        tldr = ([qa.answer_brief] if qa.answer_brief else []) + qa.answer_points[:3]

        digest = Digest(
            tldr=tldr,
            qa=qa,
            density_score=density.score,
            density_verdict=density.verdict,
            prompt_version=PROMPT_VERSION,
            model_used=f"{resp.provider}/{resp.model}",
            cost_usd=self.router.total_cost,
            tokens_in=self.router.total_tokens_in,
            tokens_out=self.router.total_tokens_out,
            open_questions=warnings,
        )
        return digest

    # ---------- map ----------

    def _map(self, chunks: list[Chunk]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        total = len(chunks)
        for chunk in chunks:
            self.progress(f"提取第 {chunk.idx + 1}/{total} 块")
            prompt = MAP_USER_TEMPLATE.format(
                index=chunk.idx + 1,
                total=total,
                **wrap_untrusted(chunk.text),
            )
            try:
                resp = self.router.complete(
                    "map", prompt, system=MAP_SYSTEM, json_mode=True
                )
                data = _parse_json(resp.text)
            except LLMError as exc:
                # 单块失败不应中断整篇，记录后继续
                self.progress(f"  第 {chunk.idx + 1} 块提取失败：{exc}")
                data = {}

            data["chunk_idx"] = chunk.idx
            data["segment_idxs"] = chunk.segment_idxs
            results.append(data)
        return results

    # ---------- reduce ----------

    def _reduce(
        self,
        title: str,
        blocks: list[dict[str, Any]],
        segments: list[Segment],
    ) -> Digest:
        self.progress("汇总为分层结论")
        blocks_json = json.dumps(blocks, ensure_ascii=False, indent=1)
        system = REDUCE_SYSTEM.replace(
            "{language}", "简体中文" if self.config.distill.language == "zh" else "English"
        )
        prompt = REDUCE_USER_TEMPLATE.format(
            title=title,
            blocks=blocks_json,
            content_start=wrap_untrusted("")["content_start"],
            content_end=wrap_untrusted("")["content_end"],
        )
        resp = self.router.complete("reduce", prompt, system=system, json_mode=True)
        data = _parse_json(resp.text)

        sections = self.config.distill.sections
        anchor_map = self._build_anchor_map(blocks, segments)

        outline = []
        if sections.outline:
            for item in _as_list(data.get("outline")):
                if not isinstance(item, dict):
                    continue
                chunk_idxs = [
                    i for i in _as_list(item.get("chunk_idxs")) if isinstance(i, int)
                ]
                outline.append(
                    OutlineItem(
                        title=str(item.get("title") or "").strip() or "未命名小节",
                        points=[str(p) for p in _as_list(item.get("points"))],
                        anchor=anchor_map.get(chunk_idxs[0]) if chunk_idxs else None,
                        chunk_idxs=chunk_idxs,
                    )
                )

        concepts = []
        if sections.concepts:
            for c in _as_list(data.get("concepts")):
                if isinstance(c, dict) and c.get("term"):
                    idx = c.get("chunk_idx")
                    concepts.append(
                        Concept(
                            term=str(c["term"]),
                            definition=str(c.get("definition") or ""),
                            anchor=anchor_map.get(idx) if isinstance(idx, int) else None,
                        )
                    )

        quotes = []
        if sections.quotes:
            for q in _as_list(data.get("quotes")):
                if isinstance(q, dict):
                    text = q.get("text")
                    idx = q.get("chunk_idx")
                else:
                    text, idx = q, None
                if text:
                    # 金句锚点让人能跳回原视频听那一句，价值比大纲锚点更高
                    quotes.append(
                        Quote(
                            text=str(text),
                            anchor=anchor_map.get(idx) if isinstance(idx, int) else None,
                        )
                    )

        return Digest(
            tldr=[str(x) for x in _as_list(data.get("tldr"))] if sections.tldr else [],
            outline=outline,
            concepts=concepts,
            quotes=quotes,
            actions=[str(x) for x in _as_list(data.get("actions"))] if sections.actions else [],
            open_questions=(
                [str(x) for x in _as_list(data.get("open_questions"))]
                if sections.open_questions else []
            ),
            contrarian=(
                [str(x) for x in _as_list(data.get("contrarian"))]
                if sections.contrarian else []
            ),
            model_used=f"{resp.provider}/{resp.model}",
            cost_usd=self.router.total_cost,
            tokens_in=self.router.total_tokens_in,
            tokens_out=self.router.total_tokens_out,
        )

    @staticmethod
    def _build_anchor_map(
        blocks: list[dict[str, Any]], segments: list[Segment]
    ) -> dict[int, str]:
        """chunk 编号 → 原文锚点（时间戳或段号），用于摘要跳转。"""
        by_idx = {s.idx: s for s in segments}
        mapping: dict[int, str] = {}
        for block in blocks:
            chunk_idx = block.get("chunk_idx")
            seg_idxs = block.get("segment_idxs") or []
            if isinstance(chunk_idx, int) and seg_idxs:
                seg = by_idx.get(seg_idxs[0])
                if seg:
                    mapping[chunk_idx] = seg.anchor
        return mapping
