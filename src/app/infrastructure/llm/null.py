"""Заглушки на случай, когда ключей нет.

Смысл в том, чтобы отсутствие ключа было обычным состоянием системы, а не
аварией. Бот обязан работать без внешних моделей: разговор ведёт claude в
отдельном процессе, а поиск по журналу переживёт и без векторов — просто станет
полнотекстовым. Поэтому заглушки не притворяются рабочими: available() честно
отвечает «нет», и вызывающий сам решает, что делать.
"""
from __future__ import annotations

from collections.abc import Sequence

from ...domain.dto import LLMReply
from .base import LLMError


class NullLLM:
    def available(self) -> bool:
        return False

    def default_model(self) -> str:
        return "—"

    async def complete(self, prompt: str, **kwargs) -> LLMReply:
        raise LLMError("языковая модель не настроена: задай LLM_API_KEY в .env")


class NullEmbedder:
    def available(self) -> bool:
        return False

    def dimensions(self) -> int:
        return 0

    def model_name(self) -> str:
        return "—"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise LLMError("эмбеддер не настроен: задай EMBEDDINGS_API_KEY в .env")
