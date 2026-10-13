"""自然语言选材测试（M2.5）。

重点覆盖规则层解析——它是纯函数、可确定性验证，
且解析错误会直接导致抓错内容（比如把「最近3个月」的 3 当成数量）。
"""

from __future__ import annotations

from datetime import date

import pytest

from distiller.config import Config, ProviderConfig, TaskConfig
from distiller.discover import clarify
from distiller.discover.models import (
    Candidate,
    Platform,
    Query,
    RankBy,
    Target,
    TargetType,
)
from distiller.discover.parser import (
    QueryParser,
    extract_date_range,
    extract_duration,
    extract_limit,
    extract_topic,
    detect_platform,
    detect_rank,
    detect_target_type,
)
from distiller.discover.planner import estimate
from distiller.discover.ranker import apply_filters, rank_candidates
from distiller.discover.topic import TopicFilter

TODAY = date(2026, 9, 16)


class TestExtractLimit:
    def test_various_forms(self) -> None:
        assert extract_limit("前20") == 20
        assert extract_limit("Top100") == 100
        assert extract_limit("10条") == 10
        assert extract_limit("前十") == 10

    def test_time_phrase_not_mistaken_for_limit(self) -> None:
        """回归测试：「最近3个月...的10条」曾把 3 当成数量。

        这类错误会静默抓错数量，不报错。
        """
        assert extract_limit("最近3个月点赞最高的10条") == 10
        assert extract_limit("2025年的15条") == 15
        assert extract_limit("最近半年前20") == 20

    def test_no_limit(self) -> None:
        assert extract_limit("某UP主的视频") is None


class TestExtractDateRange:
    def test_relative_months(self) -> None:
        dr, _ = extract_date_range("最近3个月", TODAY)
        assert dr.from_date == date(2026, 6, 18)

    def test_half_year(self) -> None:
        dr, _ = extract_date_range("最近半年", TODAY)
        assert dr.from_date == date(2026, 3, 20)

    def test_specific_year(self) -> None:
        dr, _ = extract_date_range("2025年的内容", TODAY)
        assert dr.from_date == date(2025, 1, 1)
        assert dr.to_date == date(2025, 12, 31)

    def test_specific_month(self) -> None:
        dr, _ = extract_date_range("2025年3月", TODAY)
        assert dr.from_date == date(2025, 3, 1)
        assert dr.to_date == date(2025, 3, 31)

    def test_december_boundary(self) -> None:
        """跨年边界：12 月的月末计算不能溢出。"""
        dr, _ = extract_date_range("2025年12月", TODAY)
        assert dr.to_date == date(2025, 12, 31)

    def test_none_when_unspecified(self) -> None:
        dr, _ = extract_date_range("某UP主的视频", TODAY)
        assert dr.from_date is None


class TestExtractTopic:
    def test_does_not_swallow_author_name(self) -> None:
        """回归测试：「影视飓风摄影方向」曾得到 ["影视飓风摄影"]。

        主题词吞掉博主名会导致主题筛选完全失效。
        """
        assert extract_topic("影视飓风摄影方向最近半年") == ["摄影"]

    def test_strips_leading_particles(self) -> None:
        assert extract_topic("2025年的美食方向内容") == ["美食"]

    def test_prefers_longest_known_topic(self) -> None:
        assert extract_topic("李永乐机器学习方向的视频") == ["机器学习"]

    def test_unknown_topic_falls_back(self) -> None:
        topics = extract_topic("量子计算方向的内容")
        assert topics and "量子计算" in topics[0]

    def test_no_topic(self) -> None:
        assert extract_topic("某UP主的视频前20") == []


class TestDetectors:
    def test_platform(self) -> None:
        assert detect_platform("B站的视频") is Platform.BILIBILI
        assert detect_platform("知乎专栏") is Platform.ZHIHU
        assert detect_platform("公众号文章") is Platform.WECHAT
        assert detect_platform("小红书笔记") is Platform.XHS
        assert detect_platform("某博主的内容") is Platform.UNKNOWN

    def test_rank(self) -> None:
        assert detect_rank("收藏量前100") is RankBy.FAVORITES
        assert detect_rank("播放量最高") is RankBy.PLAY
        assert detect_rank("点赞最多") is RankBy.LIKES
        assert detect_rank("最新发布") is RankBy.DATE

    def test_keyword_intent_beats_uploader(self) -> None:
        """回归测试：「B站关于RAG的视频」曾被判为 uploader，
        导致去搜不存在的 UP 主而失败。"""
        assert detect_target_type("B站关于RAG的视频") is TargetType.KEYWORD
        assert detect_target_type("搜索大模型微调") is TargetType.KEYWORD

    def test_uploader_and_favorites(self) -> None:
        assert detect_target_type("罗翔的视频") is TargetType.UPLOADER
        assert detect_target_type("某UP主的投稿") is TargetType.UPLOADER
        assert detect_target_type("我的收藏夹里前50篇") is TargetType.FAVORITES
        assert detect_target_type("知乎专栏") is TargetType.COLUMN

    def test_duration(self) -> None:
        assert extract_duration("超过10分钟").min_sec == 600
        assert extract_duration("少于5分钟").max_sec == 300
        assert extract_duration("长视频").min_sec == 600


class TestParserIntegration:
    """规则层端到端：不依赖 LLM，必须能独立处理常见指令。"""

    @pytest.fixture
    def parser(self) -> QueryParser:
        return QueryParser(router=None)

    def test_full_instruction(self, parser: QueryParser) -> None:
        q = parser.parse("B站 影视飓风 摄影方向 最近半年 播放量前20", today=TODAY)
        t = q.targets[0]
        assert t.platform is Platform.BILIBILI
        assert t.raw == "影视飓风"
        assert q.filters.topic == ["摄影"]
        assert q.rank.by is RankBy.PLAY
        assert q.limit == 20
        assert q.filters.date_range.from_date == date(2026, 3, 20)

    def test_keyword_search_name_is_clean(self, parser: QueryParser) -> None:
        q = parser.parse("B站关于RAG的视频收藏量前30", today=TODAY)
        assert q.targets[0].raw == "RAG"
        assert q.targets[0].type is TargetType.KEYWORD
        assert q.limit == 30

    def test_assumptions_are_recorded(self, parser: QueryParser) -> None:
        """自动决定必须显式记录，不能悄悄默认。"""
        q = parser.parse("B站 罗翔 的视频", today=TODAY)
        assert q.assumptions
        assert any("数量" in a for a in q.assumptions)

    def test_works_without_llm(self, parser: QueryParser) -> None:
        q = parser.parse("知乎某专栏最新10篇", today=TODAY)
        assert q.limit == 10
        assert q.rank.by is RankBy.DATE


class TestClarify:
    def test_unknown_platform_blocks(self) -> None:
        """平台猜错等于白跑，必须阻断。"""
        q = Query(targets=[Target(raw="某人", platform=Platform.UNKNOWN)])
        q = clarify.detect_issues(q)
        assert q.needs_clarify
        assert any(x.key == "platform" for x in q.blocking_questions)

    def test_favorites_ambiguity_blocks(self) -> None:
        """「收藏量」有两种解读，结果完全不同，必须问清。"""
        q = Query(
            targets=[Target(raw="某专栏", platform=Platform.ZHIHU, type=TargetType.COLUMN)],
            rank=__import__("distiller.discover.models", fromlist=["Rank"]).Rank(
                by=RankBy.FAVORITES
            ),
            raw_input="某专栏收藏量前100",
        )
        q = clarify.detect_issues(q)
        assert any(x.key == "favorites_meaning" for x in q.blocking_questions)

    def test_my_favorites_resolved_without_asking(self) -> None:
        """说了「我的收藏夹」就不该再问——能推断就不阻断。"""
        from distiller.discover.models import Rank

        q = Query(
            targets=[Target(raw="x", platform=Platform.BILIBILI)],
            rank=Rank(by=RankBy.FAVORITES),
            raw_input="我的收藏夹里前50篇",
        )
        q = clarify.detect_issues(q)
        assert not any(x.key == "favorites_meaning" for x in q.blocking_questions)
        assert q.targets[0].type is TargetType.FAVORITES

    def test_unsupported_rank_warns_not_blocks(self) -> None:
        """平台不支持某排序时应告知补偿方案，而不是报错拒绝。"""
        from distiller.discover.models import Rank

        q = Query(
            targets=[
                Target(raw="某UP", platform=Platform.BILIBILI, type=TargetType.UPLOADER)
            ],
            rank=Rank(by=RankBy.FAVORITES),
            raw_input="某UP主 被收藏最多的视频",
        )
        q = clarify.detect_issues(q)
        assert any("不支持" in a and "本地补齐" in a for a in q.assumptions)

    def test_wechat_batch_limit_disclosed(self) -> None:
        """公众号无批量接口是硬限制，应如实告知而非假装能做。"""
        q = Query(
            targets=[Target(raw="某号", platform=Platform.WECHAT, type=TargetType.KEYWORD)]
        )
        q = clarify.detect_issues(q)
        assert any(x.key == "wechat_limit" for x in q.blocking_questions)

    def test_apply_answers(self) -> None:
        q = Query(targets=[Target(raw="某人", platform=Platform.UNKNOWN)])
        q = clarify.detect_issues(q)
        q = clarify.apply_answers(q, {"platform": "1"})
        assert q.targets[0].platform is Platform.BILIBILI
        assert not q.needs_clarify


class TestTopicFilter:
    def _cands(self, titles: list[str]) -> list[Candidate]:
        return [
            Candidate(url=f"u{i}", title=t, platform="bilibili")
            for i, t in enumerate(titles)
        ]

    def test_rule_layer_needs_no_llm(self) -> None:
        """漏斗第一层必须零成本覆盖大部分情况。"""
        tf = TopicFilter(topics=["摄影"], router=None)
        cands = self._cands([
            "如何用光圈和构图拍出电影感人像",
            "风光摄影的布光技巧",
            "我的一天vlog 早八人日常",
        ])
        passed, calls = tf.apply(cands)
        assert calls == 0
        assert len(passed) == 2

    def test_exclude_words_take_priority(self) -> None:
        tf = TopicFilter(topics=["摄影"], router=None)
        cands = self._cands(["手机评测：这台拍照真的强吗"])
        passed, _ = tf.apply(cands)
        assert not passed
        assert "排除词" in (cands[0].excluded_reason or "")

    def test_scores_are_attached(self) -> None:
        """置信度必须回填，用户要能看到判定依据。"""
        tf = TopicFilter(topics=["美食"], router=None)
        cands = self._cands(["米其林餐厅探店试吃"])
        tf.apply(cands)
        assert cands[0].topic_score and cands[0].topic_score > 0
        assert cands[0].topic_reason

    def test_english_word_boundary(self) -> None:
        """英文关键词需词边界，避免 Go 命中 Google。"""
        tf = TopicFilter(topics=["编程"], router=None)
        cands = self._cands(["Googles new product launch"])
        passed, _ = tf.apply(cands)
        assert not passed

    def test_no_topic_passes_all(self) -> None:
        tf = TopicFilter(topics=[], router=None)
        cands = self._cands(["任意内容", "另一条"])
        passed, calls = tf.apply(cands)
        assert len(passed) == 2 and calls == 0


class TestFiltersAndRanking:
    def test_date_filter(self) -> None:
        from distiller.discover.models import DateRange, Filters

        q = Query(filters=Filters(date_range=DateRange(from_date=date(2026, 1, 1))))
        cands = [
            Candidate(url="a", title="新", platform="b", published=date(2026, 5, 1)),
            Candidate(url="b", title="旧", platform="b", published=date(2025, 5, 1)),
        ]
        kept, notes = apply_filters(cands, q)
        assert [c.title for c in kept] == ["新"]
        assert notes

    def test_duration_filter(self) -> None:
        from distiller.discover.models import Duration, Filters

        q = Query(filters=Filters(duration=Duration(min_sec=600)))
        cands = [
            Candidate(url="a", title="长", platform="b", duration_sec=900),
            Candidate(url="b", title="短", platform="b", duration_sec=120),
        ]
        kept, _ = apply_filters(cands, q)
        assert [c.title for c in kept] == ["长"]

    def test_exclude_keywords(self) -> None:
        from distiller.discover.models import Filters

        q = Query(filters=Filters(exclude_keywords=["直播回放"]))
        cands = [
            Candidate(url="a", title="正片内容", platform="b"),
            Candidate(url="b", title="直播回放合集", platform="b"),
        ]
        kept, _ = apply_filters(cands, q)
        assert len(kept) == 1

    def test_rank_by_play_desc(self) -> None:
        from distiller.discover.models import Rank

        q = Query(rank=Rank(by=RankBy.PLAY), limit=2)
        cands = [
            Candidate(url=f"u{i}", title=str(i), platform="b", play_count=n)
            for i, n in enumerate([100, 900, 500])
        ]
        result = rank_candidates(cands, q)
        assert [c.play_count for c in result.candidates] == [900, 500]

    def test_missing_metric_sorted_last(self) -> None:
        """缺指标的排末尾，并明确告知——不能让缺数据的混在前面。"""
        from distiller.discover.models import Rank

        q = Query(rank=Rank(by=RankBy.FAVORITES), limit=3)
        cands = [
            Candidate(url="a", title="有", platform="b", favorite_count=50),
            Candidate(url="b", title="无", platform="b"),
        ]
        result = rank_candidates(cands, q)
        assert result.candidates[0].title == "有"
        assert any("缺少该指标" in n for n in result.notes)


class TestRecencyIntent:
    """「最近 N 个视频」这类日常说法的解析。

    全部来自实际使用时发现的问题：拿「影视飓风最近3个视频」去跑，
    三处都错了——关键词粘上「最近」、排序按播放量、类型判成关键词搜索。
    """

    @pytest.fixture
    def parser(self) -> QueryParser:
        return QueryParser(router=None)

    def test_recency_sorts_by_date(self) -> None:
        """回归测试：「最近3个视频」曾按播放量排序。

        后果很实际——用户要最新 3 个，却拿到播放量最高的 3 个，
        完全是另一批视频，而且不会有任何报错提示。
        """
        assert detect_rank("影视飓风最近3个视频") is RankBy.DATE
        assert detect_rank("最近5个视频") is RankBy.DATE
        assert detect_rank("近期的视频") is RankBy.DATE

    def test_explicit_rank_beats_recency(self) -> None:
        """「最近半年 播放量前5」里的「最近」只管时间范围，排序仍按播放量。"""
        assert detect_rank("影视飓风 最近半年 播放量前5") is RankBy.PLAY
        assert detect_rank("最近一年收藏量前10") is RankBy.FAVORITES

    def test_time_range_alone_does_not_force_date_sort(self) -> None:
        """「最近半年的视频」只是限定范围，不该推翻默认排序。"""
        assert detect_rank("影视飓风最近半年的视频") is None

    def test_recency_word_stripped_from_name(self, parser: QueryParser) -> None:
        """回归测试：曾得到「影视飓风最近」这种查不到任何结果的关键词。"""
        q = parser.parse("影视飓风最近3个视频")
        assert q.targets[0].raw == "影视飓风"

    def test_platform_and_recency_stripped(self, parser: QueryParser) -> None:
        """回归测试：「X的视频」分支曾直接 return，跳过全部清洗，
        导致「B站影视飓风最近的视频」提取出「B站影视飓风最近」。"""
        q = parser.parse("B站影视飓风最近的视频")
        assert q.targets[0].raw == "影视飓风"
        assert q.targets[0].platform is Platform.BILIBILI

    def test_time_range_stripped_from_name(self, parser: QueryParser) -> None:
        q = parser.parse("影视飓风最近半年的视频")
        assert q.targets[0].raw == "影视飓风"

    def test_recency_with_count_means_uploader(self, parser: QueryParser) -> None:
        """回归测试：「影视飓风最近3个视频」曾判成关键词搜索。

        搜关键词会返回一堆别人讲该话题的视频，与要某人的投稿完全不同。
        """
        q = parser.parse("影视飓风最近3个视频")
        assert q.targets[0].type is TargetType.UPLOADER
        assert q.rank.by is RankBy.DATE
        assert q.limit == 3

    def test_explicit_search_intent_stays_keyword(self, parser: QueryParser) -> None:
        """「关于/搜索 X」是明确的全站检索意图，不能因为带「最近N个」就变成找人。"""
        for text in ("B站关于RAG的最近3个视频", "搜索RAG最近5个视频"):
            q = parser.parse(text)
            assert q.targets[0].type is TargetType.KEYWORD, text
            assert q.targets[0].raw == "RAG", text

    def test_topic_still_separated(self, parser: QueryParser) -> None:
        q = parser.parse("B站影视飓风摄影方向最近5个视频")
        assert q.targets[0].raw == "影视飓风"
        assert q.filters.topic == ["摄影"]
        assert q.rank.by is RankBy.DATE


class TestEstimate:
    def _cfg(self) -> Config:
        return Config(
            providers={
                "p": ProviderConfig(
                    type="ollama",
                    models={"chat": "m"},
                    price_per_1m={"input": 0.14, "output": 0.28},
                )
            },
            tasks={"reduce": TaskConfig(provider="p")},
        )

    def test_subtitle_much_cheaper_than_asr(self) -> None:
        """有字幕应显著快于需转写——这是字幕优先策略的量化依据。"""
        cfg = self._cfg()
        with_sub = [
            Candidate(url="a", title="x", platform="b", duration_sec=3600, has_subtitle=True)
        ]
        need_asr = [
            Candidate(url="b", title="y", platform="b", duration_sec=3600, has_subtitle=False)
        ]
        assert estimate(with_sub, cfg).est_seconds < estimate(need_asr, cfg).est_seconds

    def test_unknown_subtitle_tracked_separately(self) -> None:
        """回归测试：字幕未探测时曾展示成「有字幕 0 条」。

        实测差距 10 倍（有字幕 16s vs 未探测上限 166s），
        说成「0 条有字幕」会让人误以为确实都要转写、成本必然很高。
        """
        cfg = self._cfg()
        cands = [
            Candidate(url=f"u{i}", title="x", platform="b", duration_sec=1200)
            for i in range(3)
        ]
        est = estimate(cands, cfg)
        assert est.subtitle_unknown == 3
        assert est.with_subtitle == 0
        # 未探测计入 need_asr（保守估算），但必须能区分出来
        assert est.need_asr == 3

    def test_mixed_subtitle_states(self) -> None:
        cfg = self._cfg()
        est = estimate(
            [
                Candidate(url="a", title="x", platform="b", duration_sec=600, has_subtitle=True),
                Candidate(url="b", title="y", platform="b", duration_sec=600, has_subtitle=False),
                Candidate(url="c", title="z", platform="b", duration_sec=600),
            ],
            cfg,
        )
        assert (est.with_subtitle, est.subtitle_unknown) == (1, 1)

    def test_in_library_excluded_from_work(self) -> None:
        """已在库的应跳过，避免重复花钱。"""
        cfg = self._cfg()
        cands = [
            Candidate(url="a", title="x", platform="b", duration_sec=600, in_library=True),
            Candidate(url="b", title="y", platform="b", duration_sec=600),
        ]
        est = estimate(cands, cfg)
        assert est.already_in_library == 1
        assert est.to_process == 1
        assert any("已在库" in w for w in est.warnings)

    def test_long_asr_batch_warns(self) -> None:
        """大批量转写必须警告，否则用户会白等几小时。"""
        cfg = self._cfg()
        cands = [
            Candidate(
                url=f"u{i}", title="x", platform="b",
                duration_sec=3600, has_subtitle=False,
            )
            for i in range(20)
        ]
        est = estimate(cands, cfg)
        assert est.est_seconds > 1800
        assert any("音频转写" in w for w in est.warnings)

    def test_human_time_format(self) -> None:
        cfg = self._cfg()
        est = estimate([], cfg)
        est.est_seconds = 3725
        assert "小时" in est.human_time()


class TestKeywordSearchRegressions:
    """「B站搜索 ai infra 排名第一的视频」实测暴露的 bug。

    共同特征：**不报错，只给错结果**——这类问题比崩溃危险得多。
    """

    def _parse(self, text: str):
        from distiller.discover.parser import QueryParser

        return QueryParser(None).parse(text)

    def test_english_phrase_not_truncated_at_space(self) -> None:
        """「ai infra」曾被截成「ai」。

        字符类不含空格 + lookahead 含 \\s 导致。搜 ai 和搜 ai infra
        是完全不同的结果，且不会有任何报错提示。
        """
        assert self._parse("B站搜索 ai infra 排名第一的视频").targets[0].raw == "ai infra"
        assert self._parse("B站搜索 vector database 前5").targets[0].raw == "vector database"

    def test_english_phrase_stops_at_chinese(self) -> None:
        """英文词组遇中文修饰语要停，不能把中文吞进关键词。"""
        raw = self._parse("B站搜索 ai infra 技术的视频").targets[0].raw
        assert "技术" not in raw
        assert raw.startswith("ai")

    def test_chinese_keyword_still_works(self) -> None:
        assert self._parse("B站关于摄影的视频前10").targets[0].raw == "摄影"
        assert self._parse("B站搜索RAG的视频").targets[0].raw == "RAG"

    def test_ordinal_recognized_as_limit(self) -> None:
        """「第N」此前完全没识别，会静默取默认 20 条 —— 20 倍花费。"""
        from distiller.discover.parser import extract_limit

        assert extract_limit("排名第一的视频") == 1
        assert extract_limit("第二个视频") == 2
        assert extract_limit("第 3 条") == 3

    def test_ordinal_does_not_break_existing_limit_forms(self) -> None:
        from distiller.discover.parser import extract_limit

        assert extract_limit("收藏量前100") == 100
        assert extract_limit("最近3个月的10条") == 10
        assert extract_limit("影视飓风最近3个视频") == 3

    def test_search_rank_means_platform_relevance(self) -> None:
        """「搜索排名第一」要的是搜索框第一条（totalrank），
        不是播放量最高那条 —— 这两者通常是不同的视频。"""
        from distiller.discover.parser import detect_rank

        assert detect_rank("搜索排名第一") is RankBy.RELEVANCE
        assert detect_rank("综合排序前10") is RankBy.RELEVANCE

    def test_keyword_search_defaults_to_relevance(self) -> None:
        q = self._parse("B站搜索 ai infra 排名第一的视频")
        assert q.rank.by is RankBy.RELEVANCE
        assert q.limit == 1

    def test_uploader_keeps_play_default(self) -> None:
        """UP 主投稿场景保持播放量默认，不受关键词搜索改动影响。"""
        q = self._parse("B站UP主影视飓风摄影方向前20")
        assert q.targets[0].type is TargetType.UPLOADER
        assert q.rank.by is RankBy.PLAY

    def test_explicit_rank_still_wins(self) -> None:
        q = self._parse("B站搜索 ai infra 播放量前10")
        assert q.rank.by is RankBy.PLAY

    def test_relevance_is_native_search_order(self) -> None:
        """relevance 是 B 站搜索原生支持的排序，不该被当成
        「不支持」而抓 3 倍候选。"""
        from distiller.discover.providers.bilibili import _SEARCH_ORDER

        assert _SEARCH_ORDER[RankBy.RELEVANCE] == "totalrank"
        assert RankBy.RELEVANCE in _SEARCH_ORDER

    def test_relevance_keeps_platform_order(self) -> None:
        """相关度排序必须保持接口返回顺序，不能本地重排。"""
        from distiller.discover.models import Rank
        from distiller.discover.ranker import rank_candidates

        q = Query(rank=Rank(by=RankBy.RELEVANCE), limit=2)
        cands = [
            Candidate(url="first", title="第一", platform="b", play_count=10),
            Candidate(url="second", title="第二", platform="b", play_count=999999),
        ]
        result = rank_candidates(cands, q)
        # 播放量更高的 second 不能被排到前面
        assert result.candidates[0].url == "first"
