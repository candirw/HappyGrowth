"""配置系统测试。

重点覆盖两类容易静默出错的问题：
① 示例配置与代码模型不一致（用户按示例填了却不生效）；
② YAML 顶层重复键——后者静默覆盖前者，极难发现。
"""

from __future__ import annotations

import collections
from pathlib import Path

import pytest

from distiller.compat.fsio import read_text
from distiller.config import (
    Config,
    ConfigError,
    ProviderConfig,
    TaskConfig,
    load_config,
)

_EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"


class TestExampleConfig:
    def test_no_duplicate_top_level_keys(self) -> None:
        """YAML 重复键会静默覆盖，是最难排查的配置问题之一。"""
        text = read_text(_EXAMPLE)
        keys = [
            line.split(":")[0]
            for line in text.splitlines()
            if line and not line[0].isspace() and not line.startswith("#") and ":" in line
        ]
        dups = [k for k, c in collections.Counter(keys).items() if c > 1]
        assert not dups, f"配置存在重复顶层键: {dups}"

    def test_example_loads_and_maps_to_model(self) -> None:
        """示例配置的每个字段都必须能被代码识别，否则用户白填。"""
        cfg = load_config(_EXAMPLE)
        assert cfg.retrieval.embedding_model
        assert cfg.transcribe.model
        assert cfg.ingest.ocr_engine in ("auto", "rapidocr", "ocrmac", "off")
        assert "local" in cfg.profiles

    def test_api_keys_not_hardcoded(self) -> None:
        """示例配置绝不能含真实密钥，必须用 ${ENV} 占位。"""
        text = read_text(_EXAMPLE)
        assert "sk-" not in text
        assert "${DEEPSEEK_API_KEY}" in text


class TestProviderUsable:
    def test_cloud_provider_needs_key(self) -> None:
        assert not ProviderConfig(
            type="openai_compat", base_url="https://api.deepseek.com/v1"
        ).usable

    def test_local_endpoint_needs_no_key(self) -> None:
        """自建/本地 OpenAI 兼容端点（vLLM、LM Studio）无需认证，不该被误判不可用。"""
        assert ProviderConfig(
            type="openai_compat", base_url="http://localhost:8000/v1"
        ).usable

    def test_ollama_needs_no_key(self) -> None:
        assert ProviderConfig(type="ollama").usable

    def test_custom_type_not_blocked(self) -> None:
        """自定义 provider 类型不应因缺 key 被拦住。"""
        assert ProviderConfig(type="my_custom").usable


class TestTaskResolution:
    def test_missing_task_falls_back_to_available_provider(self) -> None:
        """未显式配置任务时应回退到可用 provider，保证开箱能跑。"""
        cfg = Config(providers={"ollama": ProviderConfig(type="ollama")})
        assert cfg.task("map").provider == "ollama"

    def test_no_usable_provider_gives_actionable_error(self) -> None:
        cfg = Config(
            providers={"deepseek": ProviderConfig(type="openai_compat", base_url="https://x/v1")}
        )
        with pytest.raises(ConfigError, match="API Key"):
            cfg.task("map")

    def test_unknown_profile_rejected(self) -> None:
        cfg = Config(
            providers={"ollama": ProviderConfig(type="ollama")},
            tasks={"map": TaskConfig(provider="ollama")},
        )
        with pytest.raises(ConfigError, match="profile"):
            cfg.task("map", "nonexistent")

    def test_model_alias_resolution(self) -> None:
        cfg = Config(
            providers={
                "p": ProviderConfig(type="ollama", models={"chat": "qwen3:14b"})
            },
            tasks={"map": TaskConfig(provider="p", model="chat")},
        )
        assert cfg.resolve_model(cfg.task("map")) == "qwen3:14b"
