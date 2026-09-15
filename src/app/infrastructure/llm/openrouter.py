"""Чат-модели через OpenRouter — реализация порта LLMClient.

Почему OpenRouter, а не прямой SDK провайдера: один ключ и один адрес дают
доступ к четырём с лишним сотням моделей разных вендоров, и смена модели —
это строчка в .env, а не новая зависимость. Формат запроса совместим с
OpenAI Chat Completions, поэтому этот же класс работает с любым другим
OpenAI-совместимым шлюзом: достаточно поменять base_url.

Эмбеддинги OpenRouter тоже умеет (baai/bge-m3 и другие), но живут они на
отдельном эндпоинте /embeddings и в каталоге /models не показываются. Считает их
отдельный клиент — openai_embedder.py, со своим ключом и адресом в настройках.

Стоимость берём из ответа (`usage.cost`), а не считаем по прайс-листу: тарифы
меняются, а врать про деньги в логах — худший вид неточности.
"""
from __future__ import annotations

import logging

from ...domain.dto import LLMReply
from ...settings import Settings
from .base import LLMError, post_json

log = logging.getLogger(__name__)


class OpenRouterClient:
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    def available(self) -> bool:
        return bool(self._s.llm_api_key)

    def default_model(self) -> str:
        return self._s.llm_model

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._s.llm_api_key}",
            "Content-Type": "application/json",
            # OpenRouter просит представляться: это влияет на рейтинги и лимиты.
            "HTTP-Referer": "https://github.com/claude-tg",
            "X-Title": "claude-tg",
        }

    async def complete(self, prompt: str, *, system: str | None = None,
                       model: str | None = None, max_tokens: int = 1024,
                       temperature: float = 0.2) -> LLMReply:
        if not self.available():
            raise LLMError("ключ к языковой модели не задан (LLM_API_KEY)")

        messages = ([{"role": "system", "content": system}] if system else [])
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": model or self._s.llm_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            # Просим посчитать стоимость на их стороне.
            "usage": {"include": True},
        }
        data = await post_json(f"{self._s.llm_base_url}/chat/completions",
                               payload, self._headers())
        try:
            choice = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"неожиданный ответ модели: {str(data)[:200]}") from exc

        usage = data.get("usage") or {}
        return LLMReply(
            text=choice.strip(),
            model=data.get("model") or payload["model"],
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            cost_usd=float(usage.get("cost") or 0.0),
        )
