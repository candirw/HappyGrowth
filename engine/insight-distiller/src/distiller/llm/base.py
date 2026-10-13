"""LLM Provider 抽象层。

设计目标：换供应商只改配置，不改业务代码。
新增供应商 = 新增一个实现文件 + 注册，不碰调用方。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, ClassVar

from ..config import Config, ProviderConfig, TaskConfig


@dataclass
class LLMResponse:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    provider: str = ""

    def cost(self, price_in: float, price_out: float) -> float:
        return (self.tokens_in * price_in + self.tokens_out * price_out) / 1_000_000


class LLMError(RuntimeError):
    """provider 调用失败。触发降级链。"""


class LLMProvider:
    """所有供应商的统一接口。"""

    type_name: ClassVar[str] = ""

    def __init__(self, name: str, config: ProviderConfig) -> None:
        self.name = name
        self.config = config

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        raise NotImplementedError

    def health(self) -> tuple[bool, str]:
        """轻量可用性检查，供 doctor 使用。"""
        return self.config.usable, "" if self.config.usable else "缺少 API Key"


_REGISTRY: dict[str, type[LLMProvider]] = {}


def register_provider(type_name: str) -> Callable[[type[LLMProvider]], type[LLMProvider]]:
    def deco(cls: type[LLMProvider]) -> type[LLMProvider]:
        cls.type_name = type_name
        _REGISTRY[type_name] = cls
        return cls

    return deco


def build_provider(name: str, config: ProviderConfig) -> LLMProvider:
    cls = _REGISTRY.get(config.type)
    if cls is None:
        raise LLMError(
            f"未知的 provider 类型: {config.type}（已注册: {', '.join(_REGISTRY)}）"
        )
    return cls(name, config)


class LLMRouter:
    """按任务路由到对应 provider，失败时自动降级。

    降级是刚需：API 超时、限流、余额不足都不该让整个消化流程中断。
    """

    def __init__(self, config: Config, profile: str | None = None) -> None:
        self.config = config
        self.profile = profile
        self._cache: dict[str, LLMProvider] = {}
        self.total_cost = 0.0
        self.total_tokens_in = 0
        self.total_tokens_out = 0

    def _provider(self, name: str) -> LLMProvider:
        if name not in self._cache:
            self._cache[name] = build_provider(name, self.config.providers[name])
        return self._cache[name]

    def _fallback_chain(self, task_name: str) -> list[TaskConfig]:
        """主 provider + 降级链。fallback 任务若已配置则追加。"""
        chain = [self.config.task(task_name, self.profile)]
        fb = self.config.tasks.get("fallback")
        if fb and fb.provider != chain[0].provider and fb.provider in self.config.providers:
            chain.append(fb)
        return chain

    def complete(
        self,
        task_name: str,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        errors: list[str] = []
        for task in self._fallback_chain(task_name):
            provider = self._provider(task.provider)
            if not provider.config.usable:
                errors.append(f"{task.provider}: 不可用（缺少 API Key）")
                continue
            model = self.config.resolve_model(task)
            try:
                resp = provider.complete(
                    prompt,
                    model=model,
                    system=system,
                    temperature=task.temperature,
                    max_tokens=max_tokens or task.max_tokens,
                    json_mode=json_mode,
                )
            except Exception as exc:  # noqa: BLE001 - 任何失败都应尝试降级
                errors.append(f"{task.provider}: {exc}")
                continue

            price = provider.config.price_per_1m
            self.total_cost += resp.cost(price.input, price.output)
            self.total_tokens_in += resp.tokens_in
            self.total_tokens_out += resp.tokens_out
            return resp

        raise LLMError(
            f"任务 {task_name} 的所有 provider 均失败：\n  " + "\n  ".join(errors)
        )
