"""Anthropic 协议实现（消息 API 与 OpenAI 不兼容，需单独适配）。"""

from __future__ import annotations

import httpx

from .base import LLMError, LLMProvider, LLMResponse, register_provider


@register_provider("anthropic")
class AnthropicProvider(LLMProvider):
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
        base = (self.config.base_url or "https://api.anthropic.com").rstrip("/")
        payload: dict = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens or 4096,  # Anthropic 要求必填
        }
        if system:
            payload["system"] = system

        try:
            resp = httpx.post(
                f"{base}/v1/messages",
                json=payload,
                headers={
                    "x-api-key": self.config.api_key or "",
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                timeout=self.config.timeout,
            )
        except httpx.RequestError as exc:
            raise LLMError(f"网络错误: {exc}") from exc

        if resp.status_code != 200:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            tokens_in=usage.get("input_tokens", 0),
            tokens_out=usage.get("output_tokens", 0),
            model=model,
            provider=self.name,
        )
