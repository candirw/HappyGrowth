"""实体抽取与归一。

**为什么必须归一**：同一概念在不同内容里写法不同
（RAG / 检索增强生成 / Retrieval-Augmented Generation），
不归一就无法建立跨内容关联，实体表也会被同义词灌满。

策略是渐进式的，不追求完美：
1. 规则层先抽（术语、英文缩写、CamelCase）—— 零成本
2. LLM 补语义实体（人物、工具、数据集）—— 复用 map 阶段结果，不额外调用
3. 别名合并：内置映射 + 编辑距离 + 用户词典
4. `mention_count < 2` 不入图谱，避免垃圾实体
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..compat.fsio import ENCODING

# 内置别名映射：中英文对照与常见简写。
# 用户词典会与之合并（用户优先）。
BUILTIN_ALIASES: dict[str, list[str]] = {
    "RAG": ["检索增强生成", "Retrieval-Augmented Generation", "retrieval augmented generation"],
    "LLM": ["大语言模型", "大模型", "Large Language Model"],
    "Transformer": ["transformer", "变换器"],
    "Embedding": ["嵌入", "向量化", "词向量", "embedding"],
    "Fine-tuning": ["微调", "fine tuning", "finetune", "微调训练"],
    "Prompt": ["提示词", "提示", "prompt engineering", "提示工程"],
    "Vector Database": ["向量数据库", "向量库", "vector db"],
    "Attention": ["注意力", "注意力机制", "self-attention", "自注意力"],
    "ASR": ["语音识别", "自动语音识别", "speech recognition"],
    "OCR": ["光学字符识别", "文字识别"],
    "BM25": ["bm25"],
    "Reranker": ["重排", "重排序", "rerank"],
}

# 停用词：这些词高频但无信息量，入库只会污染实体表
_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "have", "has",
    "our", "your", "their", "its", "not", "but", "all", "any", "can",
    "will", "may", "one", "two", "new", "use", "used", "using", "based",
    "such", "more", "most", "other", "some", "than", "then", "these",
    "we", "they", "it", "is", "are", "was", "were", "be", "been",
    "方法", "问题", "内容", "东西", "时候", "地方", "情况", "结果", "过程",
    "视频", "文章", "论文", "作者", "我们", "他们", "这个", "那个", "什么",
    "一个", "一些", "很多", "非常", "可以", "需要", "因为", "所以", "但是",
    "Figure", "Table", "Section", "Appendix", "Equation", "et", "al",
}

# 英文缩写与专有名词：连续大写、CamelCase、带连字符的技术名词
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,8}\b")
_CAMEL_RE = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b")
_HYPHEN_TERM_RE = re.compile(r"\b[A-Z][A-Za-z]*(?:-[A-Za-z]+){1,3}\b")
_MODEL_NAME_RE = re.compile(r"\b[A-Za-z]+-?\d+[A-Za-z]*\b")


@dataclass
class Entity:
    name: str
    etype: str = "concept"      # concept | tool | dataset | person | metric
    definition: str | None = None
    contexts: list[str] = field(default_factory=list)
    from_llm: bool = False


def load_user_aliases(path: Path | None = None) -> dict[str, list[str]]:
    """加载用户别名词典。越用越准。"""
    if path is None:
        path = Path(__file__).with_name("aliases.yaml")
    if not path.exists():
        return {}
    try:
        with open(path, encoding=ENCODING) as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    aliases = data.get("aliases") or {}
    return {
        str(k): [str(x) for x in (v or [])]
        for k, v in aliases.items()
    }


class EntityNormalizer:
    """把不同写法归一到规范名。"""

    def __init__(self, user_aliases: dict[str, list[str]] | None = None) -> None:
        merged: dict[str, list[str]] = {
            k: list(v) for k, v in BUILTIN_ALIASES.items()
        }
        for canonical, alias_list in (user_aliases or {}).items():
            merged.setdefault(canonical, [])
            merged[canonical] = list(
                dict.fromkeys(merged[canonical] + list(alias_list))
            )

        self.canonical_of: dict[str, str] = {}
        for canonical, alias_list in merged.items():
            self.canonical_of[canonical.lower()] = canonical
            for alias in alias_list:
                self.canonical_of[alias.lower()] = canonical
        self.aliases = merged

    def normalize(self, name: str) -> str:
        name = (name or "").strip().strip("，,。.、;；:：\"'“”()（）")
        if not name:
            return ""
        # 已知别名直接映射
        mapped = self.canonical_of.get(name.lower())
        if mapped:
            return mapped
        # 复数还原（英文）
        if len(name) > 3 and name.endswith("s") and not name.endswith("ss"):
            singular = self.canonical_of.get(name[:-1].lower())
            if singular:
                return singular
        return name

    def alias_list(self, canonical: str) -> list[str]:
        return self.aliases.get(canonical, [])

    def is_valid(self, name: str) -> bool:
        """过滤垃圾实体。"""
        if not name or len(name) < 2:
            return False
        if name.lower() in _STOPWORDS or name in _STOPWORDS:
            return False
        # 纯数字、纯标点
        if re.fullmatch(r"[\d\s\W]+", name):
            return False
        # 实体名不该是句子片段。中文实体通常 ≤ 12 字，英文按词数判断。
        if re.search(r"[\u4e00-\u9fff]", name):
            if len(name) > 14:
                return False
        else:
            if len(name.split()) > 4 or len(name) > 40:
                return False
        # 含句末标点说明是句子而非实体
        if re.search(r"[。！？；,，]", name):
            return False
        return True


def extract_rule_entities(text: str, *, max_entities: int = 40) -> list[Entity]:
    """规则层抽取。零成本，覆盖技术术语与专有名词。"""
    counts: dict[str, int] = {}
    for pattern in (_ACRONYM_RE, _CAMEL_RE, _HYPHEN_TERM_RE, _MODEL_NAME_RE):
        for match in pattern.findall(text):
            token = match.strip()
            if len(token) < 2:
                continue
            counts[token] = counts.get(token, 0) + 1

    # 出现频次高的优先——单次出现的多为偶然提及
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [
        Entity(name=name, etype="concept")
        for name, _count in ordered[:max_entities]
    ]


def entities_from_digest(payload: dict) -> list[Entity]:
    """从浓缩结果里取实体。

    **关键：不额外调用 LLM** —— concepts 字段在 map/reduce 阶段已产出，
    直接复用即可，成本为零。
    """
    entities: list[Entity] = []
    for c in payload.get("concepts") or []:
        if isinstance(c, dict) and c.get("term"):
            entities.append(
                Entity(
                    name=str(c["term"]),
                    etype="concept",
                    definition=str(c.get("definition") or "") or None,
                    from_llm=True,
                )
            )
    # 论文的数据集与基线也是有价值的实体
    experiments = payload.get("experiments") or {}
    for key, etype in (("datasets", "dataset"), ("baselines", "tool")):
        for name in experiments.get(key) or []:
            if name:
                entities.append(Entity(name=str(name), etype=etype, from_llm=True))
    return entities


def collect_entities(
    text: str,
    digest_payload: dict | None = None,
    *,
    normalizer: EntityNormalizer | None = None,
    max_entities: int = 30,
) -> list[Entity]:
    """合并规则层与 LLM 层实体，归一后去重。"""
    normalizer = normalizer or EntityNormalizer()

    merged: dict[str, Entity] = {}

    # LLM 实体优先（带定义，质量更高）
    for entity in entities_from_digest(digest_payload or {}):
        name = normalizer.normalize(entity.name)
        if not normalizer.is_valid(name):
            continue
        entity.name = name
        merged[name.lower()] = entity

    for entity in extract_rule_entities(text, max_entities=max_entities * 2):
        name = normalizer.normalize(entity.name)
        if not normalizer.is_valid(name):
            continue
        key = name.lower()
        if key in merged:
            continue
        entity.name = name
        merged[key] = entity

    deduped = _dedupe_variants(list(merged.values()))
    return deduped[:max_entities]


def _dedupe_variants(entities: list[Entity]) -> list[Entity]:
    """合并同一实体的变体。

    如 BERT 与 BERT-Large、GPT 与 GPT-3：短名是长名的前缀且长名以
    连字符或数字延伸时，视为同一模型的具体版本，保留更具体的那个。
    不处理会让实体表里出现大量近似重复项。
    """
    # 长名优先，便于用长名吸收短名
    ordered = sorted(entities, key=lambda e: (-len(e.name), e.name))
    kept: list[Entity] = []

    for entity in ordered:
        lower = entity.name.lower()
        absorbed = False
        for existing in kept:
            if _is_variant(lower, existing.name.lower()):
                # 短名带定义时把定义并入保留项，避免丢信息
                if entity.definition and not existing.definition:
                    existing.definition = entity.definition
                absorbed = True
                break
        if not absorbed:
            kept.append(entity)

    # 恢复为原顺序偏好：LLM 实体在前
    kept.sort(key=lambda e: (not e.from_llm, e.name))
    return kept


def _is_variant(shorter: str, longer: str) -> bool:
    """判断 shorter 是否为 longer 的基础名（如 bert vs bert-large）。"""
    if shorter == longer:
        return True
    if len(shorter) >= len(longer):
        short, long_ = longer, shorter
    else:
        short, long_ = shorter, longer
    if not long_.startswith(short):
        return False
    rest = long_[len(short):]
    # 仅当延伸部分是版本号或后缀修饰时才算变体
    return bool(re.fullmatch(r"[-_ ]?\d+[a-z]*|[-_ ](?:large|base|small|xl|v\d+)", rest))
