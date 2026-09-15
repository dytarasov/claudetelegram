"""Входящие файлы и голосовые, а также выдача файлов с сервера.

Всё присланное складывается в uploads/ под именем со штампом времени: так файлы
не перетирают друг друга, а claude получает обычный путь на диске и дальше
работает с ним как с любым другим файлом.
"""
from __future__ import annotations

import contextlib
import logging
import re
import time
from datetime import datetime
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from dishka.integrations.aiogram import FromDishka

from ...domain.enums import TurnSource
from ...domain.ports import SpeechToText
from ...infrastructure.stt.base import STTError
from ...infrastructure.telegram.formatting import esc
from ...infrastructure.telegram.render import shorten
from ...infrastructure.telegram.sender import MessageSender
from ...services import events
from ...services.conversation import ConversationService
from ...settings import Settings
from .common import enqueue

log = logging.getLogger(__name__)
router = Router(name="files")

# Telegram не примет документ больше 50 МБ; берём с запасом.
MAX_SEND_BYTES = 45 * 1024 * 1024


@router.message(Command("get"))
async def cmd_get(message: Message, command: CommandObject,
                  conversation: FromDishka[ConversationService],
                  sender: FromDishka[MessageSender]) -> None:
    raw = (command.args or "").strip()
    if not raw:
        await sender.send(message.chat.id, "Укажи путь: <code>/get /etc/hostname</code>")
        return
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(conversation.process.status()["cwd"]) / path
    if not path.is_file():
        await sender.send(message.chat.id, f"Нет такого файла: <code>{esc(str(path))}</code>")
        return
    if path.stat().st_size > MAX_SEND_BYTES:
        await sender.send(message.chat.id, "Файл больше 45 МБ — Telegram не пропустит.")
        return
    events.file_sent(str(path), path.stat().st_size)
    await sender.send_file(message.chat.id, path)


@router.message(F.voice | F.audio | F.video_note)
async def on_voice(message: Message, bot: FromDishka[Bot], stt: FromDishka[SpeechToText],
                   settings: FromDishka[Settings], conversation: FromDishka[ConversationService],
                   sender: FromDishka[MessageSender]) -> None:
    media = message.voice or message.audio or message.video_note
    suffix = ".oga" if message.voice else (".mp4" if message.video_note else ".audio")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    path = settings.upload_dir / f"voice-{stamp}{suffix}"

    events.voice_received(float(getattr(media, "duration", 0) or 0))
    started = time.monotonic()
    status = await sender.send(message.chat.id, "<i>распознаю…</i>",
                               reply_to_message_id=message.message_id)
    try:
        file = await bot.get_file(media.file_id)
        await bot.download_file(file.file_path, destination=path)
        text = (await stt.transcribe(path)).strip()
    except STTError as exc:
        await sender.send(message.chat.id, f"<b>Не расшифровал</b>: {esc(str(exc))}")
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("не смог расшифровать голосовое")
        await sender.send(message.chat.id, f"<b>Не расшифровал</b>: <code>{esc(str(exc))}</code>")
        return
    finally:
        path.unlink(missing_ok=True)
        if status:
            with contextlib.suppress(TelegramBadRequest):
                await bot.delete_message(message.chat.id, status.message_id)

    if not text:
        events.voice_empty()
        await sender.send(message.chat.id, "<i>тишина — ничего не распознал</i>")
        return
    events.voice_recognized(len(text), time.monotonic() - started, 0)
    if message.caption:
        text = f"{text}\n\n{message.caption}"
    await enqueue(message, conversation, sender, text, source=TurnSource.VOICE,
                  note=f"<i>{esc(shorten(text, 900))}</i>")


@router.message(F.document | F.photo | F.video)
async def on_file(message: Message, bot: FromDishka[Bot], settings: FromDishka[Settings],
                  conversation: FromDishka[ConversationService],
                  sender: FromDishka[MessageSender]) -> None:
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if message.document:
        file_id, name = message.document.file_id, (message.document.file_name or f"file-{stamp}")
    elif message.photo:
        file_id, name = message.photo[-1].file_id, f"photo-{stamp}.jpg"
    else:
        file_id, name = message.video.file_id, (message.video.file_name or f"video-{stamp}.mp4")

    # Имя из Telegram может содержать что угодно, вплоть до слэшей.
    safe_name = re.sub(r"[^\w.\-]+", "_", name)[:120]
    path = settings.upload_dir / f"{stamp}-{safe_name}"
    try:
        file = await bot.get_file(file_id)
        await bot.download_file(file.file_path, destination=path)
    except Exception as exc:  # noqa: BLE001
        await sender.send(message.chat.id, f"<b>Не скачал файл</b>: <code>{esc(str(exc))}</code>")
        return

    events.file_received(path.name, path.stat().st_size)
    prompt = f"Я прислал файл: {path}"
    if (message.caption or "").strip():
        prompt += f"\n\n{message.caption.strip()}"
    await enqueue(message, conversation, sender, prompt, source=TurnSource.FILE,
                  note=f"<code>{esc(str(path))}</code>")
