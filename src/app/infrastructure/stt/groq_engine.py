"""Распознавание через Groq Whisper: быстро, но нужен ключ и сеть."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiohttp

from ...settings import Settings
from .base import STTError, shrink_to_mp3

log = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MAX_UPLOAD = 24 * 1024 * 1024  # лимит Groq 25 МБ, оставляем запас


class GroqTranscriber:
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    @property
    def usable(self) -> bool:
        return bool(self._s.groq_api_key)

    async def transcribe(self, path: Path, language: str | None = None) -> str:
        audio = Path(path)
        if audio.stat().st_size > MAX_UPLOAD:
            audio = await shrink_to_mp3(audio)
            if audio.stat().st_size > MAX_UPLOAD:
                raise STTError("запись слишком длинная даже после сжатия")

        form = aiohttp.FormData()
        form.add_field("file", audio.read_bytes(), filename=audio.name,
                       content_type="application/octet-stream")
        form.add_field("model", self._s.groq_stt_model)
        form.add_field("response_format", "text")
        if language:
            form.add_field("language", language)
        if self._s.stt_prompt:
            form.add_field("prompt", self._s.stt_prompt)

        try:
            timeout = aiohttp.ClientTimeout(total=180)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {self._s.groq_api_key}"},
                    data=form,
                ) as resp:
                    body = (await resp.text()).strip()
                    if resp.status == 401:
                        raise STTError("Groq отклонил ключ (401) — проверь GROQ_API_KEY")
                    if resp.status == 429:
                        raise STTError("Groq: превышен лимит запросов, попробуй через минуту")
                    if resp.status >= 400:
                        raise STTError(f"Groq вернул {resp.status}: {body[:300]}")
                    return body
        except asyncio.TimeoutError as exc:
            raise STTError("Groq не ответил за 3 минуты") from exc
        except aiohttp.ClientError as exc:
            raise STTError(f"сеть до Groq недоступна: {exc}") from exc
        finally:
            if audio != Path(path):
                audio.unlink(missing_ok=True)
