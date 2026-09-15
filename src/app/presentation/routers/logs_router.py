"""Логи в чате: посмотреть последние строки или забрать файлом.

Разбор аргументов намеренно вольный — команду набирают с телефона, часто
голосом, и требовать точный синтаксис здесь незачем. Любой порядок слов:
    /logs                 последние события
    /logs 50              последние 50
    /logs ошибка          только строки со словом «ошибка»
    /logs подробно 40     подробный лог вместо ленты
    /errors               только тревожное
    /logfile подробно     прислать файлом
"""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from dishka.integrations.aiogram import FromDishka

from ...domain.errors import UserError
from ...infrastructure.telegram.formatting import esc
from ...infrastructure.telegram.render import pack
from ...infrastructure.telegram.sender import MessageSender
from ...services.logs import ALIASES, LogService
from .. import screens

router = Router(name="logs")

DEFAULT_LINES = 30
MAX_LINES = 300
# В одно сообщение влезает около 4000 символов, но лог лучше рвать с запасом:
# <pre> добавляет обрамление, а строки бывают длинными.
CHUNK_CHARS = 3400


def _parse(raw: str) -> tuple[str, int, str | None]:
    """Из свободного текста достать поток, количество строк и фильтр."""
    stream, limit, words = "события", DEFAULT_LINES, []
    for token in (raw or "").split():
        if token.isdigit():
            limit = min(int(token), MAX_LINES)
        elif token.lower() in ALIASES:
            stream = token.lower()
        else:
            words.append(token)
    return stream, limit, " ".join(words) or None


async def _dump(sender: MessageSender, chat_id: int, title: str, lines: list[str]) -> None:
    if not lines:
        await sender.send(chat_id, f"{title}\n<i>пусто — ничего не нашлось</i>")
        return
    pieces, current = [], ""
    for line in lines:
        candidate = f"{current}\n{esc(line)}" if current else esc(line)
        if len(candidate) > CHUNK_CHARS:
            pieces.append(current)
            current = esc(line)
        else:
            current = candidate
    pieces.append(current)

    await sender.send(chat_id, title)
    for piece in pieces:
        await sender.send(chat_id, f"<pre>{piece}</pre>")


@router.message(Command("logs", "log"))
async def cmd_logs(message: Message, command: CommandObject,
                   logs: FromDishka[LogService], sender: FromDishka[MessageSender]) -> None:
    name, limit, needle = _parse(command.args or "")
    try:
        stream = logs.resolve(name)
    except UserError as exc:
        await sender.send(message.chat.id, f"Ошибка: {esc(str(exc))}")
        return
    if needle is None:
        # Без фильтра показываем экран: сворачиваемый блок и кнопки.
        text, keys = await screens.logs(logs, stream, limit)
        await sender.send(message.chat.id, text, reply_markup=keys)
        return
    lines = logs.tail(stream, limit, needle)
    title = f"<b>Лог: {esc(name)}</b> · последние {len(lines)} · фильтр «{esc(needle)}»"
    await _dump(sender, message.chat.id, title, lines)


@router.message(Command("errors"))
async def cmd_errors(message: Message, command: CommandObject,
                     logs: FromDishka[LogService], sender: FromDishka[MessageSender]) -> None:
    limit = next((int(t) for t in (command.args or "").split() if t.isdigit()), DEFAULT_LINES)
    lines = logs.problems(min(limit, MAX_LINES))
    await _dump(sender, message.chat.id,
                f"<b>Тревожное в логе</b> · последние {len(lines)}", lines)


@router.message(Command("logfile"))
async def cmd_logfile(message: Message, command: CommandObject,
                      logs: FromDishka[LogService], sender: FromDishka[MessageSender]) -> None:
    name = (command.args or "события").strip()
    try:
        stream = logs.resolve(name)
        path, compressed = logs.export(stream)
    except UserError as exc:
        await sender.send(message.chat.id, f"Ошибка: {esc(str(exc))}")
        return
    await sender.send_file(message.chat.id, path)
    if compressed:
        await sender.send(message.chat.id, "<i>лог крупный — прислал сжатым</i>")


@router.message(Command("logsize"))
async def cmd_logsize(message: Message, logs: FromDishka[LogService],
                      sender: FromDishka[MessageSender]) -> None:
    sizes = logs.sizes()
    if not sizes:
        await sender.send(message.chat.id, "Логов пока нет.")
        return
    lines = ["<b>Логи</b>", ""]
    lines += [f"<code>{esc(name):8}</code> {size / 1024:.0f} КБ" for name, size in sizes.items()]
    lines.append("")
    lines.append("<code>/logs подробно 50</code> · <code>/logfile сторож</code>")
    await sender.send(message.chat.id, "\n".join(lines))
