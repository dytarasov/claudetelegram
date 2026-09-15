"""Локальный faster-whisper: без ключей и без сети, но на CPU не быстро.

Модель грузится один раз и остаётся в памяти: повторная загрузка стоит около
минуты, что для голосового сообщения неприемлемо. Отсюда и замок — модель
однопоточная, два параллельных запроса только мешают друг другу.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from ...settings import Settings
from .base import STTError

log = logging.getLogger(__name__)


class LocalWhisperTranscriber:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._model = None
        self._lock = asyncio.Lock()

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            log.info("гружу локальную модель whisper %s…", self._s.local_stt_model)
            self._model = WhisperModel(
                self._s.local_stt_model, device="cpu", compute_type="int8",
                download_root=str(self._s.root / "models"),
            )
            log.info("модель загружена")
        return self._model

    async def warm_up(self) -> None:
        """Прогрев на старте: лучше подождать здесь, чем на первом голосовом."""
        await asyncio.to_thread(self._load)

    def _run(self, path: str, language: str | None, vad: bool = True) -> str:
        segments, _ = self._load().transcribe(
            path,
            language=language or None,
            beam_size=1,       # на двух ядрах жадный поиск заметно быстрее
            vad_filter=vad,    # режем тишину, чтобы не молоть пустоту
            condition_on_previous_text=False,
            initial_prompt=self._s.local_stt_prompt or None,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

    async def transcribe(self, path: Path, language: str | None = None) -> str:
        try:
            import faster_whisper  # noqa: F401
        except ImportError as exc:
            raise STTError(
                "локальный движок не установлен и GROQ_API_KEY не задан — "
                "голосовые распознавать нечем"
            ) from exc
        async with self._lock:
            started = time.monotonic()
            try:
                text = await asyncio.to_thread(self._run, str(path), language, True)
                if not text:
                    # Детектор речи иногда выбрасывает всю запись целиком: тихий
                    # микрофон, шум, короткая фраза. Это не «тишина», это ложное
                    # срабатывание фильтра — второй заход без него обычно спасает,
                    # и стоит он ровно столько же, сколько первый.
                    log.warning("пусто после VAD — повторяю без фильтра тишины")
                    text = await asyncio.to_thread(self._run, str(path), language, False)
            except Exception as exc:  # noqa: BLE001
                raise STTError(f"локальное распознавание не удалось: {exc}") from exc
            log.info("распознал %d симв. за %.1fс (%s)", len(text),
                     time.monotonic() - started, self._s.local_stt_model)
            return text
