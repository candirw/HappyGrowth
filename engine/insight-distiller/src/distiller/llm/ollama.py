"""本地 Ollama 实现。零成本、隐私安全，用于 map 阶段或全本地 profile。"""

from __future__ import annotations

import httpx

from .base import LLMError, LLMProvider, LLMResponse, register_provider


@register_provider("ollama")
class OllamaProvider(LLMProvider):
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
        base = (self.config.base_url or "http://localhost:11434").rstrip("/")
        payload: dict = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if system:
            payload["system"] = system
        if json_mode:
            payload["format"] = "json"
        if max_tokens:
            payload["options"]["num_predict"] = max_tokens

        try:
            resp = httpx.post(
                f"{base}/api/generate", json=payload, timeout=self.config.timeout
            )
        except httpx.RequestError as exc:
            raise LLMError(f"Ollama 不可达（是否已启动？）: {exc}") from exc

        if resp.status_code != 200:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        return LLMResponse(
            text=data.get("response", ""),
            tokens_in=data.get("prompt_eval_count", 0),
            tokens_out=data.get("eval_count", 0),
            model=model,
            provider=self.name,
        )

    def health(self) -> tuple[bool, str]:
        base = (self.config.base_url or "http://localhost:11434").rstrip("/")
        try:
            resp = httpx.get(f"{base}/api/tags", timeout=3)
            if resp.status_code == 200:
                return True, ""
            return False, f"HTTP {resp.status_code}"
        except httpx.RequestError:
            return False, "服务未运行"
