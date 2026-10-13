"""问答体独立 schema。

动因：用通用摘要结构（tldr/outline/concepts）装问答体内容，
读者拿到的是一堆片段而不是答案。用户要的是
「主问题 / 简答 / 引申问题」——这是另一种产物。
"""

from __future__ import annotations

import json

import pytest

from distiller.config import Config
from distiller.distill.summarizer import Summarizer
from distiller.models import Digest, QAContent, QAPair, Segment
from distiller.review.qa import QACollection, QAItem, is_qa_title, render_markdown

FAKE_QA = {
    "main_question": "为什么 MLA 连 V 也不需要显式解压？",
    "answer_brief": "因为 W_UV 可以吸收进输出投影 W_O，V 从未被物化。",
    "answer_points": ["点一", "点二", "点三"],
    "prerequisites": ["低秩分解", "矩阵乘法结合律"],
    "follow_ups": [
        {"question": "K 为什么不能同样吸收？", "answer": "K 要参与 RoPE 旋转。"},
    ],
    "pitfalls": ["不是不计算，而是计算被重排。"],
}


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self.text = json.dumps(payload, ensure_ascii=False)
        self.provider = "fake"
        self.model = "test"


class _FakeRouter:
    total_cost = 0.0006
    total_tokens_in = 3127
    total_tokens_out = 570

    def __init__(self, payload: dict | None = None) -> None:
        self.payload = FAKE_QA if payload is None else payload
        self.tasks_called: list[str] = []

    def complete(self, task_name, prompt, **kw):
        self.tasks_called.append(task_name)
        return _FakeResp(self.payload)


class TestIsQaTitle:
    """判定要保守——误判会让通用内容拿不到大纲/概念/金句。"""

    @pytest.mark.parametrize("title", [
        "234. 为什么MLA连V也不需要显式解压？【每天一个宝藏问题】",
        "如何可视化MOE？",
        "PPO中的优势A是怎么来的",
        "Is attention all you need?",
        "MoE 的门控有哪些实现",
    ])
    def test_question_titles(self, title: str) -> None:
        assert is_qa_title(title)

    @pytest.mark.parametrize("title", [
        "跑通TritonBench测试LLM的算子生成能力",
        "影视飓风年度总结",
        "Transformer 论文精读",
        "",
    ])
    def test_non_question_titles(self, title: str) -> None:
        assert not is_qa_title(title)

    def test_series_mark_and_number_stripped_first(self) -> None:
        """系列标记与期号不该影响判定。"""
        assert is_qa_title("【每天一个宝藏问题】223. 如何可视化MOE？")


class TestRunQa:
    def _run(self, payload: dict | None = None):
        router = _FakeRouter(payload)
        s = Summarizer(Config(), router)
        d = s.run_qa(
            "234. 为什么MLA连V也不需要显式解压？【每天一个宝藏问题】",
            [Segment(idx=0, text="字" * 600)],
        )
        return d, router

    def test_fills_qa_content(self) -> None:
        d, _ = self._run()
        assert d.qa is not None
        assert d.qa.answer_brief
        assert len(d.qa.answer_points) == 3
        assert len(d.qa.follow_ups) == 1
        assert d.qa.follow_ups[0].answer

    def test_uses_dedicated_task_name(self) -> None:
        """用 qa_card 而非 reduce：reduce 默认配 reasoner，
        批量跑 200+ 期时成本差好几倍。"""
        _, router = self._run()
        assert router.tasks_called == ["qa_card"]

    def test_also_fills_tldr(self) -> None:
        """tldr 要一起填，否则 list/show/检索 看不到内容。"""
        d, _ = self._run()
        assert d.tldr
        assert d.qa.answer_brief in d.tldr[0]

    def test_single_call_not_map_reduce(self) -> None:
        """问答体单期约 1000 字，分块会切断推导且成本翻倍。"""
        _, router = self._run()
        assert len(router.tasks_called) == 1

    def test_drops_followup_without_question(self) -> None:
        payload = dict(FAKE_QA, follow_ups=[
            {"question": "", "answer": "孤立的答案"},
            {"question": "有效问题", "answer": "有效答案"},
        ])
        d, _ = self._run(payload)
        assert len(d.qa.follow_ups) == 1
        assert d.qa.follow_ups[0].question == "有效问题"

    def test_empty_llm_response_does_not_crash(self) -> None:
        """LLM 返回空或字段缺失时退化为标题，不该抛异常。"""
        d, _ = self._run({})
        assert d.qa is not None
        assert d.qa.main_question

    def test_records_cost(self) -> None:
        d, _ = self._run()
        assert d.cost_usd > 0
        assert d.model_used == "fake/test"

    def test_rejects_empty_segments(self) -> None:
        from distiller.llm import LLMError

        s = Summarizer(Config(), _FakeRouter())
        with pytest.raises(LLMError):
            s.run_qa("标题", [])


class TestQaRendering:
    def _item(self, **kw) -> QAItem:
        base = dict(
            source_id=2,
            question="为什么 MLA 连 V 也不需要显式解压？",
            points=["点一", "点二"],
            url="https://b.com/x",
            order=234,
            brief="因为 W_UV 可以吸收进 W_O。",
            prerequisites=["低秩分解"],
            follow_ups=[("K 为什么不能？", "K 要参与 RoPE。")],
            pitfalls=["不是不计算。"],
        )
        base.update(kw)
        return QAItem(**base)

    def _render(self, **kw) -> str:
        return render_markdown(
            QACollection(title="T", items=[self._item(**kw)]), toc=False
        )

    def test_brief_comes_first_and_bold(self) -> None:
        """一句话直答要最显眼——读者扫一眼就能判断懂没懂。"""
        md = self._render()
        assert "**答：因为 W_UV 可以吸收进 W_O。**" in md
        assert md.index("**答：") < md.index("点一")

    def test_renders_follow_ups_with_answers(self) -> None:
        md = self._render()
        assert "引申" in md
        assert "K 为什么不能？" in md
        assert "K 要参与 RoPE。" in md

    def test_renders_prerequisites_and_pitfalls(self) -> None:
        md = self._render()
        assert "前置：低秩分解" in md
        assert "⚠ 不是不计算。" in md

    def test_omits_empty_sections(self) -> None:
        """没有的部分不留空标题，否则文档全是空壳。"""
        md = self._render(prerequisites=[], follow_ups=[], pitfalls=[])
        assert "引申" not in md
        assert "前置" not in md
        assert "⚠" not in md

    def test_source_link_always_present(self) -> None:
        """一切结论可溯源。"""
        assert "https://b.com/x" in self._render()


class TestBuildCollectionPrefersQa:
    """有 qa 时优先用它，没有才退化。"""

    class _FakeDB:
        def __init__(self, digest) -> None:
            self._digest = digest

        def get_source(self, sid):
            from distiller.models import ContentType, Source

            return Source(
                id=sid,
                content_type=ContentType.VIDEO,
                platform="bilibili",
                url="https://b.com/1",
                title="234. 为什么X？【每天一个宝藏问题】",
                author="海安雨",
                duration_sec=200,
            )

        def get_latest_digest(self, sid):
            return self._digest

        def get_segments(self, sid):
            return [Segment(idx=0, text="原文" * 50)]

    def test_uses_qa_when_available(self) -> None:
        from distiller.review.qa import build_collection

        digest = Digest(
            tldr=["通用摘要句"],
            qa=QAContent(
                main_question="为什么X？",
                answer_brief="因为Y。",
                answer_points=["要点"],
                follow_ups=[QAPair(question="引申？", answer="答。")],
            ),
        )
        coll = build_collection(self._FakeDB(digest), [1])
        assert coll.items[0].brief == "因为Y。"
        assert coll.items[0].follow_ups == [("引申？", "答。")]
        assert coll.extractive_count == 0

    def test_falls_back_to_tldr(self) -> None:
        from distiller.review.qa import build_collection

        coll = build_collection(self._FakeDB(Digest(tldr=["摘要句"])), [1])
        assert coll.items[0].points == ["摘要句"]
        assert not coll.items[0].brief
        assert coll.extractive_count == 0

    def test_falls_back_to_extractive(self) -> None:
        from distiller.review.qa import build_collection

        coll = build_collection(self._FakeDB(None), [1])
        assert coll.extractive_count == 1
        assert coll.items[0].extractive
