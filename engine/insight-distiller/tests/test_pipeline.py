"""端到端测试：用 fake provider 验证完整链路，不依赖网络与 API Key。

这类测试是刚需：LLM 相关代码若从未被执行过，
交付时等于把风险留给用户。
"""

from __future__ import annotations

import json

import pytest

from distiller.config import Config, ProviderConfig, TaskConfig
from distiller.distill import Summarizer
from distiller.llm import LLMRouter, register_provider
from distiller.llm.base import LLMProvider, LLMResponse
from distiller.models import Segment


@register_provider("fake")
class FakeProvider(LLMProvider):
    """按 prompt 内容返回符合 schema 的假响应。"""

    calls: list[str] = []

    def complete(self, prompt, *, model, system=None, temperature=0.2,
                 max_tokens=None, json_mode=False):
        FakeProvider.calls.append(prompt[:40])

        # reduce 阶段的 prompt 含「内容标题」
        if "内容标题" in prompt:
            payload = {
                "tldr": ["结论一：可复现的评分优于 LLM 主观打分", "结论二：引文关系是零成本关联"],
                "outline": [
                    {"title": "核心方法", "points": ["map-reduce 分层"], "chunk_idxs": [0]}
                ],
                "concepts": [{"term": "RAG", "definition": "检索增强生成"}],
                "quotes": [{"text": "信息密度决定要不要读原文"}],
                "actions": ["先跑 doctor 检查平台能力"],
                "open_questions": [],
                "contrarian": ["更长的上下文并不总是更好"],
            }
        else:
            payload = {
                "points": ["本块论点"],
                "evidence": ["某项指标提升 3.2%"],
                "terms": [{"term": "RAG", "definition": "检索增强生成"}],
                "quotes": ["一句金句"],
                "contrarian": [],
            }

        text = json.dumps(payload, ensure_ascii=False)
        return LLMResponse(
            text=text, tokens_in=100, tokens_out=50, model=model, provider=self.name
        )

    def health(self):
        return True, ""


@pytest.fixture
def config() -> Config:
    return Config(
        providers={"fake": ProviderConfig(type="fake", models={"chat": "fake-1"})},
        tasks={
            "map": TaskConfig(provider="fake"),
            "reduce": TaskConfig(provider="fake"),
        },
    )


@pytest.fixture
def segments() -> list[Segment]:
    return [
        Segment(
            idx=i,
            text=(
                f"第 {i} 段。因为模型上下文有限，所以需要分块处理。"
                f"实验显示准确率提升 {i + 1}.5%，因此这个方法有效。"
            ),
            start_ms=i * 30_000,
            end_ms=(i + 1) * 30_000,
        )
        for i in range(8)
    ]


def test_summarizer_end_to_end(config: Config, segments: list[Segment]) -> None:
    FakeProvider.calls.clear()
    router = LLMRouter(config)
    digest = Summarizer(config, router).run("测试内容", segments)

    assert len(digest.tldr) == 2
    assert digest.contrarian == ["更长的上下文并不总是更好"]
    assert digest.concepts[0].term == "RAG"
    assert digest.density_score is not None
    assert digest.tokens_in > 0
    # map 阶段每块调一次 + reduce 一次
    assert len(FakeProvider.calls) >= 2


def test_outline_anchor_maps_to_timestamp(config: Config, segments: list[Segment]) -> None:
    """大纲必须能回溯到原文时间戳，这是可溯源的基础。"""
    digest = Summarizer(config, LLMRouter(config)).run("测试", segments)
    assert digest.outline
    assert digest.outline[0].anchor == "00:00"


def test_fallback_when_primary_fails() -> None:
    """主 provider 失败必须自动降级，不能中断整条流程。"""

    @register_provider("always_fail")
    class FailProvider(LLMProvider):
        def complete(self, prompt, **kwargs):
            raise RuntimeError("模拟 API 超时")

    cfg = Config(
        providers={
            "bad": ProviderConfig(type="always_fail"),
            "fake": ProviderConfig(type="fake", models={"chat": "fake-1"}),
        },
        tasks={
            "reduce": TaskConfig(provider="bad"),
            "fallback": TaskConfig(provider="fake"),
        },
    )
    resp = LLMRouter(cfg).complete("reduce", "内容标题：x")
    assert resp.provider == "fake"


def test_profile_overrides_provider() -> None:
    cfg = Config(
        providers={
            "a": ProviderConfig(type="fake", models={"chat": "m-a"}),
            "b": ProviderConfig(type="fake", models={"chat": "m-b"}),
        },
        tasks={"map": TaskConfig(provider="a")},
        profiles={"alt": {"map": "b"}},
    )
    assert cfg.task("map").provider == "a"
    assert cfg.task("map", "alt").provider == "b"


class TestLocalMediaImport:
    """本地录音/视频导入。

    此前 `distill add 录音.m4a` 报「没有可用的采集器」——
    Whisper 引擎本就支持吃文件路径，缺的只是入口，
    而那个错误完全看不出真正原因。
    """

    def test_recognizes_audio_suffixes(self) -> None:
        from distiller.pipeline import Pipeline

        for name in ("a.mp3", "a.m4a", "a.wav", "a.flac", "a.opus", "A.MP3"):
            assert Pipeline.is_media_file(name), name

    def test_recognizes_video_suffixes(self) -> None:
        from distiller.pipeline import Pipeline

        for name in ("a.mp4", "a.mkv", "a.mov", "a.webm"):
            assert Pipeline.is_media_file(name), name

    def test_rejects_non_media(self) -> None:
        from distiller.pipeline import Pipeline

        for name in ("a.txt", "a.md", "a.pdf", "a", "a.json"):
            assert not Pipeline.is_media_file(name), name

    def test_missing_file_gives_clear_error(self, tmp_path) -> None:
        from distiller.ingest import FetchError
        from distiller.pipeline import Pipeline

        cfg = Config()
        cfg.storage.data_dir = str(tmp_path)
        with Pipeline(cfg, progress=lambda _m: None) as p:
            with pytest.raises(FetchError, match="不存在"):
                p.from_media(str(tmp_path / "nope.mp3"))

    def test_unsupported_suffix_lists_options(self, tmp_path) -> None:
        """报错要列出支持的格式，而不是让用户猜。"""
        from distiller.ingest import FetchError
        from distiller.pipeline import Pipeline

        bad = tmp_path / "x.xyz"
        bad.write_text("x", encoding="utf-8")
        cfg = Config()
        cfg.storage.data_dir = str(tmp_path)
        with Pipeline(cfg, progress=lambda _m: None) as p:
            with pytest.raises(FetchError, match="不支持的媒体格式"):
                p.from_media(str(bad))

    def test_text_file_errors_are_friendly(self, tmp_path) -> None:
        """回归测试：文件不存在时曾抛 FileNotFoundError 栈。"""
        from distiller.ingest import FetchError, PasteAdapter
        from distiller.config import IngestConfig

        ad = PasteAdapter(IngestConfig())
        with pytest.raises(FetchError, match="不存在"):
            ad.from_file(str(tmp_path / "nope.txt"))

        with pytest.raises(FetchError, match="目录"):
            ad.from_file(str(tmp_path))

        binary = tmp_path / "b.bin"
        binary.write_bytes(b"\x00\x01\xff\xfe" * 64)
        with pytest.raises(FetchError, match="无法以文本方式读取"):
            ad.from_file(str(binary))
