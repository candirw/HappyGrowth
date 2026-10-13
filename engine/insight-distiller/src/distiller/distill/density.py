"""信息密度评分。

**刻意不用 LLM 打分**：LLM 主观评分不可复现、不稳定、且要额外花钱。
这里用可解释的启发式指标，同一内容永远得到同一分数，
且每一项都能向用户解释"为什么判定这是水文"。

评分维度（总分 100）：
- 实体/术语密度：专有名词越多，信息量越大
- 事实与数字密度：带数字、年份、比例的内容更硬
- 论证结构完整度：出现"因为/所以/但是/相反"等逻辑连接词
- 有效句占比：过短句子多说明碎片化
- 重复率惩罚：口播内容常反复讲同一件事
- 口水词与营销词惩罚：这是低密度内容最强的信号
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 口水词与营销话术：中文自媒体内容的典型填充物
_FILLER_WORDS = [
    "那么", "然后呢", "就是说", "其实呢", "对吧", "大家好", "话不多说",
    "咱们", "这个东西", "怎么说呢", "你知道吗", "对不对", "是不是",
    "点赞", "关注", "订阅", "投币", "三连", "评论区", "下期视频",
    "家人们", "宝子们", "废话不多说", "干货满满", "绝对干货",
    "震惊", "必看", "史上最", "唯一", "彻底改变", "颠覆",
]

_LOGIC_MARKERS = [
    "因为", "所以", "因此", "但是", "然而", "相反", "反之", "首先",
    "其次", "最后", "综上", "换句话说", "也就是说", "关键在于",
    "原因是", "导致", "取决于", "前提是", "反过来", "对比",
]

# 英文逻辑连接词。必须单独维护：中英混排内容很常见（技术博客、论文），
# 若只统计中文标记，英文内容的「论证结构」会恒为 0，密度被系统性低估约 20 分。
_LOGIC_MARKERS_EN = [
    "because", "therefore", "thus", "hence", "however", "although",
    "whereas", "in contrast", "on the contrary", "as a result",
    "consequently", "for example", "for instance", "in other words",
    "that is", "the key is", "depends on", "leads to", "due to",
    "first,", "second,", "finally", "in summary", "note that",
]

# 英文口水词与营销话术
_FILLER_WORDS_EN = [
    "you know", "kind of", "sort of", "basically", "literally",
    "like i said", "at the end of the day", "game-changing",
    "must-read", "mind-blowing", "revolutionary", "subscribe",
    "smash that like", "don't forget to", "in this video",
]

_NUM_RE = re.compile(r"\d+(?:\.\d+)?%?")
_YEAR_RE = re.compile(r"(19|20)\d{2}\s*年?")
# 术语信号：英文缩写、CamelCase、中英混排的技术名词
_TERM_RE = re.compile(r"\b[A-Z][A-Za-z0-9]{1,}\b|\b[A-Z]{2,}\b")

# 句子切分：中文标点直接切；英文 .!? 需后跟空白才切，
# 这样 "3.14" / "0.5%" 这类小数不会被误切成两句。
# 若不处理英文句号，英文长段落会被当成单个句子，
# 导致「有效句占比」虚高、「重复率」失真——中英文评分标准必须一致。
_SENT_SPLIT = re.compile(r"[。！？；;\n]+|(?<=[.!?])\s+")


@dataclass
class DensityResult:
    score: float
    verdict: str
    details: dict[str, float]

    @property
    def should_detail(self) -> bool:
        return self.score >= 20


def _ratio(hits: int, total: int) -> float:
    return hits / total if total else 0.0


def score_density(text: str) -> DensityResult:
    text = text or ""
    lowered = text.lower()
    length = max(len(text), 1)
    sentences = [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]
    sent_count = max(len(sentences), 1)

    # 1. 术语密度（每千字出现次数，上限归一）
    terms = set(_TERM_RE.findall(text))
    term_density = min(len(terms) / (length / 1000 + 1) / 12, 1.0)

    # 2. 事实与数字密度
    numbers = _NUM_RE.findall(text) + _YEAR_RE.findall(text)
    num_density = min(len(numbers) / (length / 1000 + 1) / 10, 1.0)

    # 3. 论证结构（中英双语统计，缺一不可）
    logic_hits = sum(text.count(m) for m in _LOGIC_MARKERS)
    logic_hits += sum(lowered.count(m) for m in _LOGIC_MARKERS_EN)
    logic_density = min(logic_hits / sent_count / 0.35, 1.0)

    # 4. 有效句占比。阈值按语言自适应：
    #    中文 15 字已是完整表述，英文 15 字符只有约 3 个词，需更高阈值。
    han_count = sum(1 for ch in text[:4000] if "\u4e00" <= ch <= "\u9fff")
    is_chinese = han_count / max(len(text[:4000]), 1) > 0.15
    min_sent_len = 15 if is_chinese else 40
    effective = sum(1 for s in sentences if len(s) >= min_sent_len)
    effective_ratio = _ratio(effective, sent_count)

    # 5. 重复率惩罚：用句子去重比例衡量
    unique_ratio = _ratio(len({s[:24] for s in sentences}), sent_count)

    # 6. 口水词惩罚（中英双语）
    filler_hits = sum(text.count(w) for w in _FILLER_WORDS)
    filler_hits += sum(lowered.count(w) for w in _FILLER_WORDS_EN)
    filler_penalty = min(filler_hits / (length / 1000 + 1) / 8, 1.0)

    raw = (
        term_density * 22
        + num_density * 20
        + logic_density * 20
        + effective_ratio * 20
        + unique_ratio * 18
    )
    score = max(0.0, min(100.0, raw - filler_penalty * 25))

    if score >= 65:
        verdict = "信息密度高，值得读原文"
    elif score >= 40:
        verdict = "信息密度中等，看浓缩版即可"
    elif score >= 20:
        verdict = "信息密度偏低，只看结论"
    else:
        verdict = "疑似水文，建议丢弃"

    return DensityResult(
        score=round(score, 1),
        verdict=verdict,
        details={
            "术语密度": round(term_density, 3),
            "数字事实密度": round(num_density, 3),
            "论证结构": round(logic_density, 3),
            "有效句占比": round(effective_ratio, 3),
            "非重复率": round(unique_ratio, 3),
            "口水词惩罚": round(filler_penalty, 3),
        },
    )
