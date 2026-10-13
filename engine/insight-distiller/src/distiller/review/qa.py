"""Q&A 合集导出。

**适用场景**：很多知识类系列内容本身就是问答体
（「每天一个宝藏问题」「XX 十问」「面试题精讲」），
标题即问题、正文即回答。这类内容导出成
「问题 → 要点 → 出处」的合集，比逐篇摘要更贴合原始结构，
也更适合当速查手册用。

**与 vault 导出的区别**：
- vault 是按「内容」组织，一篇一个文件，强调关联与图谱；
- 本模块按「问题」组织，全部汇成一个文档，强调可检索与可通读。

**无 LLM 时也能用**：浓缩结果（tldr）缺失时退化为抽取式摘要——
从正文里挑信息密度高的句子。这保证了没有配 LLM 的用户
依然能得到一份可用的文档，而不是空文件。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..compat.fsio import write_text
from ..models import Digest, Source
from ..store import Database

# 标题里的系列标记与序号，展示时要剥掉
_SERIES_MARK = re.compile(r"[【\[]([^】\]]{2,30})[】\]]")
_LEADING_NUM = re.compile(r"^\s*[\[\(]?\d+[\.\)\]]?\s*")
_TRAILING_TAG = re.compile(r"[（(](新版|重制|修订)[）)]\s*$")


@dataclass
class QAItem:
    source_id: int
    question: str
    points: list[str]
    url: str
    order: int | None = None          # 系列内编号，如 234
    duration_sec: int | None = None
    extractive: bool = False          # 是否为无 LLM 时的抽取式退化
    # 以下来自 QAContent（有 LLM 时才有）
    brief: str = ""                              # 一句话直答
    prerequisites: list[str] = field(default_factory=list)
    follow_ups: list[tuple[str, str]] = field(default_factory=list)  # (问题, 简答)
    pitfalls: list[str] = field(default_factory=list)


@dataclass
class QACollection:
    title: str
    items: list[QAItem] = field(default_factory=list)
    author: str | None = None
    series: str | None = None
    extractive_count: int = 0

    @property
    def total(self) -> int:
        return len(self.items)


def is_qa_title(title: str) -> bool:
    """标题是否本身就是一个问题。

    判据故意保守——**误判成问答体会让通用内容拿不到大纲/概念/金句**，
    代价比漏判大。因此要求有明确的疑问信号：
    - 问号（中英文皆可）
    - 疑问词开头或结尾（如何/怎么/为什么/是什么/能不能）

    「如何评价这部电影」这类也会命中，但它确实也适合问答式浓缩，
    不算误判。
    """
    text = _SERIES_MARK.sub("", title or "").strip()
    text = _LEADING_NUM.sub("", text).strip()
    if not text:
        return False
    if "?" in text or "？" in text:
        return True
    return bool(
        re.search(
            r"(如何|怎么|怎样|为什么|为何|是什么|有什么|能不能|是否|哪些|多少)",
            text,
        )
    )


def parse_question(title: str) -> tuple[str, int | None, str | None]:
    """从标题拆出「问题正文 / 系列序号 / 系列名」。

    「234. 为什么MLA连V也不需要显式解压？（新版）【每天一个宝藏问题】」
      → ("为什么MLA连V也不需要显式解压？", 234, "每天一个宝藏问题")
    """
    series = None
    if m := _SERIES_MARK.search(title):
        series = m.group(1).strip()
    text = _SERIES_MARK.sub("", title).strip()

    order = None
    if m := re.match(r"^\s*[\[\(]?(\d+)[\.\)\]]?\s*", text):
        order = int(m.group(1))
    text = _LEADING_NUM.sub("", text)
    text = _TRAILING_TAG.sub("", text).strip()

    return text or title.strip(), order, series


def split_clauses(text: str) -> list[str]:
    """把文本切成便于阅读的短句。

    **关键约束：AI 字幕通常完全没有标点。**
    实测 B 站 AI 自动字幕是「今天的问题是如何可视化moo e先要问如何...」
    这样连续 130 字无标点的段落，按句号/逗号切会得不到任何结果。

    因此分两种策略：
    - 有标点 → 按标点切（正常文章、CC 人工字幕）
    - 无标点 → 按口语连接词切（「然后」「所以」「那么」「因为」…），
      这些词在中文口语里几乎总出现在语义边界上。
    """
    text = (text or "").strip()
    if not text:
        return []

    if re.search(r"[。！？；，]", text):
        # 注意：末句若以句号结尾，split 会产生尾部空串，
        # 但中间的短句同样会被 >=8 过滤掉，因此下限放宽到 6，
        # 避免「因此需要混合。」这类简短但完整的结论被丢掉。
        parts = re.split(r"[。！？；\n]|，(?=.{12,})", text)
        return [p.strip() for p in parts if len(p.strip()) >= 6]

    # 无标点：在连接词前插入切点。保留连接词在后半句开头，读起来更自然。
    marked = re.sub(
        r"(?<=.{10})(然后|所以|因此|那么|但是|不过|而且|接下来|也就是说|"
        r"这就是|本质上|注意|其实|比如说|如果|因为|由于|最后|首先|其次)",
        r"\n\1",
        text,
    )
    parts = [p.strip() for p in marked.split("\n") if len(p.strip()) >= 8]
    # 切不开（整段就是一句）时原样返回，交给上层截断
    return parts or [text]


def extractive_points(segments: list[str], *, limit: int = 4) -> list[str]:
    """无 LLM 时的退化摘要：按信息密度挑句子。

    这不是「摘要」，只是**有依据的节选**——因此调用方必须标注
    extractive=True，不能让用户误以为是模型生成的结论。

    打分偏好：含结论/因果词、含对比或定义信号、长度适中。
    """
    scored: list[tuple[float, int, str]] = []
    for i, text in enumerate(segments):
        for s in split_clauses(text):
            # 长度下限只用来挡明显的碎片。
            # 曾设成 16，结果把「所以优势的本质是比平均好多少」（14 字）
            # 这类**最有价值的结论句**滤掉了，反而留下长而空的句子。
            # 语义完整性应当靠下面的悬垂惩罚判断，不是靠长度一刀切。
            if not (10 <= len(s) <= 100):
                continue
            score = 0.0
            # 结论与因果信号：这类句子通常是答案本身
            if re.search(r"(所以|因此|本质上|本质是|关键|核心|原因|就是说|也就是|意味着|这就是)", s):
                score += 2.2
            # 定义与对比信号
            if re.search(r"(不需要|不用|区别|不同|相当于|等价|可以理解为|其实就是)", s):
                score += 1.6
            # 含术语/公式通常是干货
            if re.search(r"[A-Za-z]{2,}|√|×|÷|[0-9]", s):
                score += 0.7
            # 首段开头多为「今天的问题是…」式重述，价值低。
            # 标题里已有问题，文档里再放一遍纯属占位。
            if i == 0:
                score -= 1.2
            if re.search(r"^(今天的?问题|本期|这期|大家好|上期)", s):
                score -= 10.0
            # 引流话术直接淘汰。用足够大的负值而非 -3.0：
            # 长句的长度加分最高 +1.0，加上术语分可能把 -3.0 抵消掉，
            # 导致「关注我点赞三连」出现在正式文档里。
            if re.search(r"(关注|点赞|三连|下期|评论区|私信|本期就到这里|我们下期)", s):
                score -= 100.0
            # 悬垂结尾（以连接词/助词/系动词收尾）说明句子被切断了。
            # 「所以反向传播训练的效果是」「这就是transformer」这类
            # 读者看了拿不到任何结论，惩罚要足够重。
            if re.search(r"(的|了|和|与|而|就|把|对|在|是|为|即|比|从|用|让)$", s):
                score -= 4.0
            # 过短且无实质内容的（「这就是transformer」）
            if len(s) < 14 and not re.search(r"[，。]", s):
                score -= 1.5
            score += min(len(s), 50) / 50.0
            scored.append((score, i, s))

    scored.sort(key=lambda x: (-x[0], x[1]))
    picked: list[tuple[int, str]] = []
    seen: set[str] = set()
    for _, idx, s in scored:
        key = s[:12]
        if key in seen:
            continue
        seen.add(key)
        picked.append((idx, s))
        if len(picked) >= limit:
            break
    # 按原文顺序输出，保持逻辑连贯
    picked.sort(key=lambda x: x[0])
    return [s for _, s in picked]


def build_collection(
    db: Database,
    source_ids: list[int],
    *,
    title: str = "Q&A 合集",
) -> QACollection:
    """按给定内容构建 Q&A 合集。缺浓缩结果时退化为抽取式。"""
    coll = QACollection(title=title)
    authors: dict[str, int] = {}
    series_names: dict[str, int] = {}

    for sid in source_ids:
        source = db.get_source(sid)
        if source is None:
            continue

        question, order, series = parse_question(source.title)
        if series:
            series_names[series] = series_names.get(series, 0) + 1
        if source.author:
            authors[source.author] = authors.get(source.author, 0) + 1

        digest: Digest | None = db.get_latest_digest(sid)
        points: list[str] = []
        extractive = False
        brief = ""
        prerequisites: list[str] = []
        follow_ups: list[tuple[str, str]] = []
        pitfalls: list[str] = []

        qa = digest.qa if digest else None
        if qa and (qa.answer_brief or qa.answer_points):
            # 问答 schema：这才是用户要的形态
            if qa.main_question:
                question = qa.main_question
            brief = qa.answer_brief
            points = list(qa.answer_points)
            prerequisites = list(qa.prerequisites)
            pitfalls = list(qa.pitfalls)
            follow_ups = [
                (f.question, f.answer) for f in qa.follow_ups if f.question
            ]
        elif digest and digest.tldr:
            # 通用摘要：能用但不是问答形态
            points = list(digest.tldr)
        else:
            segs = [s.text for s in db.get_segments(sid)]
            points = extractive_points(segs)
            extractive = True
            coll.extractive_count += 1

        coll.items.append(
            QAItem(
                brief=brief,
                prerequisites=prerequisites,
                follow_ups=follow_ups,
                pitfalls=pitfalls,
                source_id=sid,
                question=question,
                points=points,
                url=source.url,
                order=order,
                duration_sec=source.duration_sec,
                extractive=extractive,
            )
        )

    # 有系列编号则按编号排，否则保持传入顺序
    if all(i.order is not None for i in coll.items) and coll.items:
        coll.items.sort(key=lambda i: i.order or 0)

    if authors:
        coll.author = max(authors, key=lambda k: authors[k])
    if series_names:
        coll.series = max(series_names, key=lambda k: series_names[k])
    return coll


# 主题分组规则。按题目关键词归类，让长系列可按兴趣跳读。
# 顺序即优先级：一题命中多个主题时取第一个匹配的。
TOPIC_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("位置编码 / RoPE", ("rope", "位置编码", "旋转", "欧拉", "三角函数", "外推", "yarn", "ntk")),
    ("注意力机制", ("注意力", "attention", "softmax", "掩码", "因果", "gqa", "mqa", "mha", "滑动窗口")),
    ("KV Cache 与 MLA", ("kv cache", "kvcache", "kv", "mla", "缓存", "解压")),
    ("MoE 专家混合", ("moe", "专家", "门控", "路由", "gating")),
    ("强化学习 / PPO / GRPO", ("ppo", "grpo", "dpo", "强化学习", "优势", "价值网络", "奖励", "gae", "策略梯度", "rlhf")),
    ("剪枝与量化", ("剪枝", "量化", "obd", "obs", "obq", "int8", "smoothquant", "离群值", "压缩", "稀疏")),
    ("微调 / LoRA / SFT", ("lora", "sft", "微调", "思维链", "adapter", "外积")),
    ("采样与解码策略", ("temperature", "topk", "top-k", "top p", "topp", "采样", "贪婪", "解码策略", "beam")),
    ("算子与硬件加速", ("simd", "双缓冲", "网格步进", "张量计算", "cuda", "kernel", "算子", "gemm", "gemv", "访存", "共享内存", "warp")),
    ("推理与部署优化", ("prefill", "decode", "投机", "吞吐", "并行", "batch", "推理")),
    ("训练与反向传播", ("反向传播", "梯度", "训练", "损失", "优化器", "学习率", "初始化", "归一化", "norm", "广播", "归约")),
    ("多模态", ("clip", "ocr", "视觉", "图像", "多模态", "vit")),
    ("数学基础", ("泰勒", "拉格朗日", "矩阵", "向量", "导数", "凸", "概率", "分块")),
    ("Transformer 基础", ("transformer", "ffn", "残差", "embedding", "词表", "tokenizer")),
]


def group_items(items: list[QAItem]) -> list[tuple[str, list[QAItem]]]:
    """按主题分组。未命中任何规则的归入「其他」。

    长系列（200+ 题）平铺目录几乎没法用，按主题分组才能按兴趣跳读。
    """
    buckets: dict[str, list[QAItem]] = {name: [] for name, _ in TOPIC_GROUPS}
    others: list[QAItem] = []

    for item in items:
        q = item.question.lower()
        for name, keywords in TOPIC_GROUPS:
            if any(k in q for k in keywords):
                buckets[name].append(item)
                break
        else:
            others.append(item)

    out = [(name, buckets[name]) for name, _ in TOPIC_GROUPS if buckets[name]]
    if others:
        out.append(("其他", others))
    return out


def render_markdown(coll: QACollection, *, toc: bool = True, group: bool = True) -> str:
    """渲染成 Markdown。

    结构：元信息 → 目录（题量大时按主题分组）→ 每题（问题/要点/出处）。
    出处必须带链接与编号，保证「一切结论可溯源」。
    """
    lines: list[str] = []
    meta = [f"# {coll.title}", ""]

    info: list[str] = []
    if coll.series:
        info.append(f"系列：{coll.series}")
    if coll.author:
        info.append(f"作者：{coll.author}")
    info.append(f"共 {coll.total} 题")
    info.append(datetime.now(timezone.utc).astimezone().strftime("导出于 %Y-%m-%d"))
    meta.append(" · ".join(info))
    meta.append("")

    if coll.extractive_count:
        # 必须显眼声明，否则用户会把节选当成模型生成的结论
        meta.append(
            f"> ⚠ 其中 {coll.extractive_count} 题为**原文节选**而非模型生成的摘要"
            f"（未配置 LLM 时的退化模式）。内容取自字幕原文，可能不完整。"
        )
        meta.append("")
    lines.extend(meta)

    # 题号 → 锚点序号，供分组目录引用
    anchor_of = {id(item): i for i, item in enumerate(coll.items, start=1)}

    if toc and coll.total > 3:
        # 超过 30 题时平铺目录已经不好用，改按主题分组
        groups = group_items(coll.items) if (group and coll.total > 30) else []
        if groups:
            lines.append("## 按主题浏览")
            lines.append("")
            for name, items in groups:
                lines.append(f"**{name}**（{len(items)} 题）")
                lines.append("")
                for item in items:
                    num = f"{item.order}. " if item.order else ""
                    lines.append(
                        f"- [{num}{item.question}](#q{anchor_of[id(item)]})"
                    )
                lines.append("")
        else:
            lines.append("## 目录")
            lines.append("")
            for i, item in enumerate(coll.items, start=1):
                num = f"{item.order}. " if item.order else ""
                lines.append(f"{i}. [{num}{item.question}](#q{i})")
            lines.append("")

    lines.append("---")
    lines.append("")

    for i, item in enumerate(coll.items, start=1):
        num = f"{item.order}. " if item.order else ""
        lines.append(f'<a id="q{i}"></a>')
        lines.append("")
        lines.append(f"## Q{i}. {num}{item.question}")
        lines.append("")

        # 一句话直答放最前面并加粗：读者扫过去就能判断自己懂没懂，
        # 不懂再往下看要点。这是「简略版本」的核心。
        if item.brief:
            lines.append(f"**答：{item.brief}**")
            lines.append("")

        if item.prerequisites:
            lines.append(f"<sub>前置：{'、'.join(item.prerequisites)}</sub>")
            lines.append("")

        if item.points:
            for p in item.points:
                lines.append(f"- {p}")
            lines.append("")
        elif not item.brief:
            lines.append("- *（未能提取要点，请查看原片）*")
            lines.append("")

        if item.pitfalls:
            for p in item.pitfalls:
                lines.append(f"> ⚠ {p}")
            lines.append("")

        if item.follow_ups:
            lines.append("**引申**")
            lines.append("")
            for q, a in item.follow_ups:
                lines.append(f"- **{q}**" + (f" — {a}" if a else ""))
            lines.append("")

        bits = [f"[原片]({item.url})", f"#{item.source_id}"]
        if item.duration_sec:
            bits.append(f"{item.duration_sec // 60}分{item.duration_sec % 60}秒")
        if item.extractive:
            bits.append("原文节选")
        lines.append(f"<sub>{' · '.join(bits)}</sub>")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def export_qa(
    db: Database,
    source_ids: list[int],
    out_path: Path,
    *,
    title: str = "Q&A 合集",
    toc: bool = True,
) -> QACollection:
    coll = build_collection(db, source_ids, title=title)
    write_text(out_path, render_markdown(coll, toc=toc))
    return coll


__all__ = [
    "QACollection",
    "QAItem",
    "TOPIC_GROUPS",
    "build_collection",
    "export_qa",
    "extractive_points",
    "group_items",
    "is_qa_title",
    "parse_question",
    "render_markdown",
    "split_clauses",
]
