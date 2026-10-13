"""OpenAI 兼容协议实现。

覆盖 DeepSeek / OpenAI / Kimi / 任意兼容端点——
这是加新供应商成本最低的路径：只需在配置里加一段 base_url。
"""

from __future__ import annotations

import httpx

from ..config import ProviderConfig
from .base import LLMError, LLMProvider, LLMResponse, register_provider


@register_provider("openai_compat")
class OpenAICompatProvider(LLMProvider):
    def __init__(self, name: str, config: ProviderConfig) -> None:
        super().__init__(name, config)
        if not config.base_url:
            raise LLMError(f"provider {name} 缺少 base_url")

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
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        url = self.config.base_url.rstrip("/") + "/chat/completions"
        try:
            resp = httpx.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.config.timeout,
            )
        except httpx.RequestError as exc:
            raise LLMError(f"网络错误: {exc}") from exc

        if resp.status_code != 200:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise LLMError(f"响应格式异常: {str(data)[:300]}") from exc

        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
            model=model,
            provider=self.name,
        )
