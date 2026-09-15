"""Общее для распознавателей речи."""
from __future__ import annotations

import asyncio
from pathlib import Path

from ...domain.errors import AppError


class STTError(AppError):
    """Не смогли расшифровать голосовое — текст ошибки уходит человеку."""


async def shrink_to_mp3(path: Path) -> Path:
    """Сжать аудио в 16 кГц моно mp3.

    Нужно и для лимита загрузки Groq, и просто чтобы не гонять по сети
    несжатый ogg. Возвращает новый файл — удалять его должен вызывающий.
    """
    out = path.with_suffix(".stt.mp3")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(path), "-ac", "1", "-ar", "16000", "-b:a", "32k", str(out),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0 or not out.exists():
        raise STTError(f"ffmpeg не смог сжать аудио: {err.decode()[-200:]}")
    return out
