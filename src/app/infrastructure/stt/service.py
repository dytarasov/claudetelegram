"""Распознавание речи как один порт с двумя движками и понятной политикой.

Политика: есть ключ Groq — идём в Groq, он быстрее; сорвалось и разрешён откат —
доделываем локально. Роутеру всё это знать не нужно, он видит только transcribe().
"""
from __future__ import annotations

import logging
from pathlib import Path

from ...settings import Settings
from .base import STTError
from .groq_engine import GroqTranscriber
from .local_engine import LocalWhisperTranscriber

log = logging.getLogger(__name__)


class SpeechService:
    """Реализация порта SpeechToText."""

    def __init__(self, settings: Settings, groq: GroqTranscriber,
                 local: LocalWhisperTranscriber) -> None:
        self._s = settings
        self._groq = groq
        self._local = local

    async def transcribe(self, path: Path) -> str:
        """Отдаёт ровно то, что услышала модель.

        Термины чинит ConversationService: правило одно и то же для любого
        движка, а решение «как подать реплику модели» — не дело распознавалки.
        """
        language = self._s.stt_language
        if self._groq.usable:
            try:
                return await self._groq.transcribe(path, language)
            except STTError as exc:
                if not self._s.local_stt_fallback:
                    raise
                log.warning("Groq не справился (%s) — падаю на локальный движок", exc)
        return await self._local.transcribe(path, language)

    def backend_name(self) -> str:
        return "groq" if self._groq.usable else f"local:{self._s.local_stt_model}"

    async def warm_up(self) -> None:
        if not self._groq.usable:
            await self._local.warm_up()
