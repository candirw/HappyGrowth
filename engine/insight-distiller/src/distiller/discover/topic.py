"""主题方向判定：两阶段漏斗。

**为什么必须分两阶段**：「美食方向」「摄影方向」这类语义判定，
平台元数据里通常没有这个维度。但对 300 条候选逐个跑 LLM 太慢太贵
（一次调用约 0.5s + 成本），漏斗把 LLM 用量压到约 1/10。

阶段一（免费）：分区/标签/标题关键词 + 同义词表打分，覆盖约 80%
阶段二（LLM）：仅对阈值带内的边缘案例批量判定

用户可维护 topics.yaml 词典，越用越准。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..compat.fsio import ENCODING
from ..llm import LLMError, LLMRouter
from .models import Candidate

# 内置同义词表。用户词典会与之合并（用户优先）。
BUILTIN_TOPICS: dict[str, dict[str, list[str]]] = {
    "摄影": {
        "include": ["摄影", "镜头", "构图", "布光", "相机", "调色", "后期",
                     "光圈", "快门", "人像", "风光", "胶片", "修图", "拍摄"],
        "exclude": ["手机评测", "开箱"],
    },
    "美食": {
        "include": ["美食", "探店", "试吃", "食谱", "料理", "厨房", "做饭",
                     "烘焙", "菜谱", "餐厅", "小吃", "食材"],
        "exclude": [],
    },
    "编程": {
        "include": ["编程", "代码", "开发", "算法", "框架", "重构", "调试",
                     "Python", "Java", "Go", "Rust", "前端", "后端", "数据库"],
        "exclude": [],
    },
    "理财": {
        "include": ["理财", "基金", "股票", "债券", "利率", "资产配置", "保险",
                     "定投", "收益率", "通胀", "美债", "存款", "投资"],
        "exclude": ["带货", "广告"],
    },
    "健身": {
        "include": ["健身", "增肌", "减脂", "训练", "卧推", "深蹲", "有氧",
                     "蛋白", "拉伸", "体态"],
        "exclude": [],
    },
    "机器学习": {
        "include": ["机器学习", "深度学习", "神经网络", "模型", "训练", "梯度",
                     "Transformer", "大模型", "LLM", "微调", "推理", "数据集"],
        "exclude": [],
    },
}


@dataclass
class TopicVerdict:
    matched: bool
    score: float
    reason: str


@dataclass
class TopicFilter:
    """两阶段主题判定。"""

    topics: list[str]
    router: LLMRouter | None = None
    dictionary: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    strict: bool = False

    # 阈值带：高于 high 直接入选，低于 low 直接淘汰，中间交 LLM
    high: float = 0.5
    low: float = 0.12

    def __post_init__(self) -> None:
        merged = {k: dict(v) for k, v in BUILTIN_TOPICS.items()}
        for topic, spec in (self.dictionary or {}).items():
            if topic in merged:
                merged[topic] = {
                    "include": list(
                        dict.fromkeys(
                            merged[topic].get("include", []) + spec.get("include", [])
                        )
                    ),
                    "exclude": list(
                        dict.fromkeys(
                            merged[topic].get("exclude", []) + spec.get("exclude", [])
                        )
                    ),
                }
            else:
                merged[topic] = {
                    "include": spec.get("include", []),
                    "exclude": spec.get("exclude", []),
                }
        self.dictionary = merged
        if self.strict:
            self.high = 0.35
            self.low = 0.2

    # ---------- 阶段一：免费粗筛 ----------

    def rule_score(self, cand: Candidate) -> TopicVerdict:
        text = f"{cand.title} {cand.author or ''}"
        best_score = 0.0
        best_reason = ""

        for topic in self.topics:
            spec = self.dictionary.get(topic, {})
            includes = spec.get("include") or [topic]
            excludes = spec.get("exclude") or []

            if any(_contains(text, kw) for kw in excludes):
                return TopicVerdict(False, 0.0, f"命中排除词（{topic}）")

            hits = [kw for kw in includes if _contains(text, kw)]
            if not hits:
                continue

            # 命中数越多越可信；标题短则单个命中权重更高
            score = min(1.0, 0.42 + 0.22 * (len(hits) - 1))
            if _contains(cand.title, topic):
                score = min(1.0, score + 0.2)
            if score > best_score:
                best_score = score
                best_reason = f"标题命中 {'、'.join(hits[:3])}"

        if best_score == 0.0:
            return TopicVerdict(False, 0.0, "标题未命中主题关键词")
        return TopicVerdict(best_score >= self.high, best_score, best_reason)

    # ---------- 阶段二：LLM 判定边缘案例 ----------

    def apply(self, candidates: list[Candidate]) -> tuple[list[Candidate], int]:
        """返回 (通过筛选的候选, LLM 调用次数)。"""
        if not self.topics:
            return candidates, 0

        passed: list[Candidate] = []
        borderline: list[Candidate] = []

        for cand in candidates:
            verdict = self.rule_score(cand)
            cand.topic_score = verdict.score
            cand.topic_reason = verdict.reason

            if verdict.score >= self.high:
                passed.append(cand)
            elif verdict.score >= self.low:
                borderline.append(cand)
            else:
                cand.excluded_reason = f"主题不匹配（{verdict.reason}）"

        llm_calls = 0
        if borderline and self.router is not None:
            llm_calls = self._llm_judge(borderline, passed)
        elif borderline:
            # 无 LLM 时保守放行边缘案例，但标注低置信度
            for cand in borderline:
                cand.topic_reason = (cand.topic_reason or "") + "（未经语义确认）"
                passed.append(cand)

        return passed, llm_calls

    def _llm_judge(
        self, borderline: list[Candidate], passed: list[Candidate]
    ) -> int:
        """批量判定：一次请求处理多条，把成本压到 1/N。"""
        calls = 0
        batch_size = 20
        topics_text = "、".join(self.topics)

        for start in range(0, len(borderline), batch_size):
            batch = borderline[start : start + batch_size]
            listing = "\n".join(
                f"{i}. {c.title}" for i, c in enumerate(batch, start=1)
            )
            prompt = (
                f"判断下列内容标题是否属于「{topics_text}」主题。\n\n"
                f"{listing}\n\n"
                '只输出 JSON：{"results": [{"n": 序号, "match": true/false, '
                '"confidence": 0-1}]}'
            )
            try:
                resp = self.router.complete(
                    "map",
                    prompt,
                    system="你是内容分类助手，只依据标题判断主题归属。只输出 JSON。",
                    json_mode=True,
                )
                calls += 1
                data = _parse_json(resp.text)
            except LLMError:
                # LLM 不可用时保守放行，标注未确认
                for cand in batch:
                    cand.topic_reason = (cand.topic_reason or "") + "（语义判定失败，已保留）"
                    passed.append(cand)
                continue

            verdicts = {
                int(r["n"]): r
                for r in (data.get("results") or [])
                if isinstance(r, dict) and str(r.get("n", "")).isdigit()
            }
            for i, cand in enumerate(batch, start=1):
                v = verdicts.get(i)
                if v is None:
                    cand.topic_reason = (cand.topic_reason or "") + "（未返回判定，已保留）"
                    passed.append(cand)
                    continue
                if v.get("match"):
                    conf = float(v.get("confidence") or 0.6)
                    cand.topic_score = max(cand.topic_score or 0.0, conf)
                    cand.topic_reason = f"语义判定匹配（置信度 {conf:.2f}）"
                    passed.append(cand)
                else:
                    cand.excluded_reason = "语义判定不匹配"
        return calls


def _contains(text: str, keyword: str) -> bool:
    """中文直接子串匹配；英文加词边界，避免 Go 命中 Google。"""
    if not keyword:
        return False
    if re.search(r"[\u4e00-\u9fff]", keyword):
        return keyword in text
    return re.search(rf"\b{re.escape(keyword)}\b", text, re.IGNORECASE) is not None


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
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


def load_user_topics(path: Path | None = None) -> dict[str, dict[str, list[str]]]:
    """加载用户主题词典。文件不存在则返回空字典。"""
    if path is None:
        path = Path(__file__).with_name("topics.yaml")
    if not path.exists():
        return {}
    try:
        with open(path, encoding=ENCODING) as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    topics = data.get("topics") or {}
    return {
        str(k): {
            "include": [str(x) for x in (v or {}).get("include", [])],
            "exclude": [str(x) for x in (v or {}).get("exclude", [])],
        }
        for k, v in topics.items()
    }
