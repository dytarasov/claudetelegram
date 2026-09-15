"""Облачное распознавание речи через OpenAI-совместимый /audio/transcriptions.

По умолчанию — OpenRouter (тем же ключом и аккаунтом, что и чат-модели): у него
с июля 2026 есть этот эндпоинт с Whisper и другими STT-моделями. Формат — обычный
multipart с файлом, ровно как у OpenAI/Groq, поэтому провайдер меняется одними
настройками (адрес, ключ, имя модели), а код один.

Единственное место, где мы ходим в STT-провайдера по сети. Вся защита от его
капризов (ключ, лимит, таймаут, сеть) — здесь; наружу отдаём либо текст, либо
STTError с человеческим сообщением.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import aiohttp

from ...settings import Settings
from .base import STTError, shrink_to_mp3

log = logging.getLogger(__name__)

# Разумный потолок загрузки: и по сети не гонять лишнего, и уложиться в лимиты
# провайдеров. Больше — сжимаем в mp3 16кГц моно.
MAX_UPLOAD = 24 * 1024 * 1024


def extract_text(body: str) -> str:
    """Достать расшифровку из ответа.

    Обычно это JSON `{"text": "..."}`, но на response_format=text приходит голый
    текст — терпим оба варианта, чтобы смена провайдера/формата не роняла STT.
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body.strip()
    if isinstance(data, dict) and data.get("text") is not None:
        return str(data["text"]).strip()
    return body.strip()


class CloudTranscriber:
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    @property
    def usable(self) -> bool:
        return bool(self._s.stt_key)

    @property
    def _url(self) -> str:
        return self._s.stt_base_url.rstrip("/") + "/audio/transcriptions"

    async def transcribe(self, path: Path, language: str | None = None) -> str:
        audio = Path(path)
        if audio.stat().st_size > MAX_UPLOAD:
            audio = await shrink_to_mp3(audio)
            if audio.stat().st_size > MAX_UPLOAD:
                raise STTError("запись слишком длинная даже после сжатия")

        form = aiohttp.FormData()
        form.add_field("file", audio.read_bytes(), filename=audio.name,
                       content_type="application/octet-stream")
        form.add_field("model", self._s.stt_model)
        form.add_field("response_format", "json")
        if language:
            form.add_field("language", language)

        try:
            timeout = aiohttp.ClientTimeout(total=180)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self._url,
                    headers={"Authorization": f"Bearer {self._s.stt_key}"},
                    data=form,
                ) as resp:
                    body = (await resp.text()).strip()
                    if resp.status == 401:
                        raise STTError("STT-провайдер отклонил ключ (401) — проверь ключ")
                    if resp.status == 429:
                        raise STTError("STT-провайдер: лимит запросов, попробуй через минуту")
                    if resp.status >= 400:
                        raise STTError(f"STT вернул {resp.status}: {body[:300]}")
                    return extract_text(body)
        except asyncio.TimeoutError as exc:
            raise STTError("STT-провайдер не ответил за 3 минуты") from exc
        except aiohttp.ClientError as exc:
            raise STTError(f"сеть до STT недоступна: {exc}") from exc
        finally:
            if audio != Path(path):
                audio.unlink(missing_ok=True)
