"""Q&A 合集导出测试。

重点是两件事：
1. 标题解析（系列标记/序号/重制后缀要正确剥离）
2. 无标点字幕的切句 —— B 站 AI 自动字幕完全没有标点，
   按句号切会一条都得不到，这是实测踩到的坑。
"""

from __future__ import annotations

from distiller.review.qa import (
    QACollection,
    QAItem,
    extractive_points,
    parse_question,
    render_markdown,
    split_clauses,
)


class TestParseQuestion:
    def test_full_form(self) -> None:
        q, order, series = parse_question(
            "234. 为什么MLA连V也不需要显式解压？（新版）【每天一个宝藏问题】"
        )
        assert q == "为什么MLA连V也不需要显式解压？"
        assert order == 234
        assert series == "每天一个宝藏问题"

    def test_strips_remake_suffix(self) -> None:
        q, _, _ = parse_question("223. 如何可视化MOE？(新版)【每天一个宝藏问题】")
        assert q == "如何可视化MOE？"

    def test_bracket_series(self) -> None:
        q, order, series = parse_question("[1]. 跑通TritonBench测试LLM的算子生成能力")
        assert order == 1
        assert "TritonBench" in q

    def test_plain_title(self) -> None:
        q, order, series = parse_question("一个没有编号的普通标题")
        assert q == "一个没有编号的普通标题"
        assert order is None
        assert series is None


class TestSplitClauses:
    def test_unpunctuated_subtitle(self) -> None:
        """AI 字幕零标点是常态，必须按口语连接词切。"""
        text = (
            "今天的问题是如何可视化moe先要问如何可视化transformer"
            "然后乘以V相当于加权平均算出来O然后FFN让向量先变长后变短"
        )
        parts = split_clauses(text)
        assert len(parts) >= 3
        assert all(len(p) >= 8 for p in parts)

    def test_punctuated_text(self) -> None:
        text = "向量检索擅长语义匹配。但在专有名词上召回很差。因此需要混合。"
        parts = split_clauses(text)
        assert len(parts) == 3

    def test_never_returns_empty_for_nonempty_input(self) -> None:
        """切不开时原样返回，不能丢内容。"""
        assert split_clauses("短文本无法切分但也不能丢") != []

    def test_empty_input(self) -> None:
        assert split_clauses("") == []
        assert split_clauses("   ") == []


class TestExtractivePoints:
    SEGS = [
        "今天的问题是PPO中的优势A是怎么来的这是一个重述性的开头",
        "A等于奖励模型打的得分减去价值网络猜的平均分所以优势的本质是比平均好多少",
        "因此这个方法可以让训练更稳定而且不需要额外的标注数据",
        "好了本期就到这里关注我点赞三连下期再见",
    ]

    def test_picks_conclusion_sentences(self) -> None:
        pts = extractive_points(self.SEGS)
        assert pts
        joined = " ".join(pts)
        assert "本质" in joined or "因此" in joined

    def test_drops_promotional_tail(self) -> None:
        """「关注我点赞三连」这类引流内容必须排除。"""
        pts = extractive_points(self.SEGS)
        assert not any("三连" in p or "点赞" in p for p in pts)

    def test_preserves_original_order(self) -> None:
        """按原文顺序输出，保持逻辑连贯。"""
        pts = extractive_points(self.SEGS, limit=3)
        positions = []
        for p in pts:
            for i, seg in enumerate(self.SEGS):
                if p[:10] in seg:
                    positions.append(i)
                    break
        assert positions == sorted(positions)

    def test_respects_limit(self) -> None:
        assert len(extractive_points(self.SEGS, limit=2)) <= 2

    def test_no_dangling_fragments(self) -> None:
        """不该出现「所以我们不考虑前面的」这类语义不完整的残句。

        判据是**不以连接词/助词结尾**，而非长度——
        长度一刀切会误杀「所以本质是比平均好多少」这类短而完整的结论。
        """
        pts = extractive_points(self.SEGS)
        assert all(not p.endswith(("的", "和", "与", "而", "把", "对")) for p in pts)


class TestRenderMarkdown:
    def _coll(self, extractive: int = 0) -> QACollection:
        return QACollection(
            title="测试合集",
            series="每天一个宝藏问题",
            author="海安雨",
            extractive_count=extractive,
            items=[
                QAItem(
                    source_id=2, question="问题一", points=["要点A", "要点B"],
                    url="https://b.com/1", order=1, duration_sec=185,
                    extractive=bool(extractive),
                ),
                QAItem(
                    source_id=3, question="问题二", points=["要点C"],
                    url="https://b.com/2", order=2,
                ),
            ],
        )

    def test_includes_source_links(self) -> None:
        """一切结论可溯源：每题必须带原片链接与库内编号。"""
        md = render_markdown(self._coll())
        assert "https://b.com/1" in md
        assert "#2" in md

    def test_declares_extractive_mode(self) -> None:
        """节选模式必须显眼声明，否则用户会当成模型摘要。"""
        md = render_markdown(self._coll(extractive=1))
        assert "原文节选" in md
        assert "⚠" in md

    def test_no_warning_when_all_llm_generated(self) -> None:
        assert "⚠" not in render_markdown(self._coll(extractive=0))

    def test_metadata_present(self) -> None:
        md = render_markdown(self._coll())
        assert "每天一个宝藏问题" in md
        assert "海安雨" in md
        assert "共 2 题" in md

    def test_empty_points_has_placeholder(self) -> None:
        coll = QACollection(
            title="t",
            items=[QAItem(source_id=1, question="q", points=[], url="u")],
        )
        assert "未能提取要点" in render_markdown(coll)


class TestTopicGrouping:
    """主题分组：长系列（200+ 题）平铺目录没法用。"""

    def _items(self, questions: list[str]) -> list[QAItem]:
        return [
            QAItem(source_id=i, question=q, points=["x"], url=f"u{i}", order=i)
            for i, q in enumerate(questions, start=1)
        ]

    def test_groups_by_topic(self) -> None:
        from distiller.review.qa import group_items

        items = self._items([
            "RoPE如何从2维推广到任意偶数维？",
            "为什么除以√dk是在给注意力升温？",
            "PPO中的优势A是怎么来的？",
            "为什么把权重置0就是在剪枝？",
        ])
        groups = dict(group_items(items))
        assert "位置编码 / RoPE" in groups
        assert "注意力机制" in groups
        assert "强化学习 / PPO / GRPO" in groups
        assert "剪枝与量化" in groups

    def test_unmatched_goes_to_others(self) -> None:
        from distiller.review.qa import group_items

        groups = dict(group_items(self._items(["完全无关的一个问题"])))
        assert "其他" in groups

    def test_no_item_lost(self) -> None:
        """分组不能丢题——这是最基本的正确性。"""
        from distiller.review.qa import group_items

        items = self._items([
            "RoPE和欧拉公式的关系？", "MoE如何路由？", "无法归类的问题",
            "LoRA为何像向量外积？", "SIMD如何加速张量计算？",
        ])
        total = sum(len(v) for _, v in group_items(items))
        assert total == len(items)

    def test_each_item_in_exactly_one_group(self) -> None:
        """一题命中多个主题时只归第一个，不能重复出现。"""
        from distiller.review.qa import group_items

        # 同时含 kv cache 与 attention 关键词
        items = self._items(["滑动窗口注意力的KV Cache如何用环形缓存？"])
        groups = group_items(items)
        assert sum(len(v) for _, v in groups) == 1

    def test_grouped_toc_used_for_long_collection(self) -> None:
        """题量大才分组；短合集用平铺目录更直接。"""
        long_coll = QACollection(title="t", items=self._items(
            [f"RoPE问题{i}" for i in range(35)]
        ))
        assert "按主题浏览" in render_markdown(long_coll)

        short_coll = QACollection(title="t", items=self._items(
            ["RoPE问题一", "RoPE问题二", "RoPE问题三", "RoPE问题四"]
        ))
        md = render_markdown(short_coll)
        assert "## 目录" in md
        assert "按主题浏览" not in md

    def test_grouped_anchors_are_valid(self) -> None:
        """分组目录的锚点必须指向真实存在的标题锚点。"""
        import re

        coll = QACollection(title="t", items=self._items(
            [f"注意力问题{i}" for i in range(35)]
        ))
        md = render_markdown(coll)
        linked = set(re.findall(r"\(#(q\d+)\)", md))
        defined = set(re.findall(r'<a id="(q\d+)"></a>', md))
        assert linked and linked <= defined
