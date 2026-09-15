"""Команды самодопила: посмотреть версии, откатиться, закрепить, проверить здоровье.

Здесь только перевод «команда → вызов сервиса → человеческий текст». Вся логика
выкатки и отката живёт в services/selfupdate.py, а физический откат делает
сторож снаружи процесса — иначе он не пережил бы перезапуск.
"""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from dishka.integrations.aiogram import FromDishka

from ...domain.dto import RollbackCommand
from ...domain.enums import TriggerKind
from ...domain.errors import AppError
from ...infrastructure.telegram.formatting import esc
from ...infrastructure.telegram.sender import MessageSender
from ...services.health import HealthService
from ...services.selfupdate import SelfUpdateService
from .. import screens

router = Router(name="selfupdate")

@router.message(Command("versions"))
async def cmd_versions(message: Message, selfupdate: FromDishka[SelfUpdateService],
                       sender: FromDishka[MessageSender]) -> None:
    text, keys = await screens.versions(selfupdate)
    await sender.send(message.chat.id, text, reply_markup=keys)


@router.message(Command("rollback"))
async def cmd_rollback(message: Message, command: CommandObject,
                       selfupdate: FromDishka[SelfUpdateService],
                       sender: FromDishka[MessageSender]) -> None:
    target = (command.args or "").strip() or "stable"
    try:
        result = await selfupdate.rollback(RollbackCommand(
            target=target, chat_id=message.chat.id, trigger=TriggerKind.USER,
            reason=f"/rollback {target}".strip(),
        ))
    except AppError as exc:
        await sender.send(
            message.chat.id,
            f"Не понял версию <code>{esc(target)}</code>\n<code>{esc(str(exc))}</code>\n"
            "Что есть — /versions",
        )
        return
    if not result.ok:
        await sender.send(message.chat.id, esc(result.message))
        return
    await sender.send(
        message.chat.id,
        f"<b>Откатываюсь</b>: {esc(result.message)}\n"
        "<i>перезапуск через несколько секунд, контекст сессии сохранится</i>",
    )


@router.message(Command("stable"))
async def cmd_stable(message: Message, command: CommandObject,
                     selfupdate: FromDishka[SelfUpdateService],
                     sender: FromDishka[MessageSender]) -> None:
    ref = (command.args or "").strip() or "HEAD"
    try:
        sha = selfupdate.mark_stable(ref)
    except AppError as exc:
        await sender.send(message.chat.id, f"Ошибка: <code>{esc(str(exc))}</code>")
        return
    await sender.send(message.chat.id,
                      f"<b>Стабильной</b> считается <code>{esc(sha[:7])}</code>\n"
                      "<i>именно сюда вернёт /rollback</i>")


@router.message(Command("diag"))
async def cmd_diag(message: Message, health: FromDishka[HealthService],
                   selfupdate: FromDishka[SelfUpdateService],
                   sender: FromDishka[MessageSender]) -> None:
    text, keys = await screens.diagnostics(health, selfupdate)
    await sender.send(message.chat.id, text, reply_markup=keys)


@router.message(Command("preflight"))
async def cmd_preflight(message: Message, selfupdate: FromDishka[SelfUpdateService],
                        sender: FromDishka[MessageSender]) -> None:
    """Прогнать проверки, ничего не выкатывая, — полезно перед ручной правкой."""
    await sender.send(message.chat.id, "<i>проверяю: компиляция, импорт, настройки, тесты…</i>")
    report = await selfupdate.preflight()
    lines = ["<b>Preflight</b>"]
    lines += [f"{esc(name)}: {'прошло' if ok else 'ПРОВАЛ'}" for name, ok in report.checks.items()]
    if report.problems:
        lines += [""] + [f"<code>{esc(p)}</code>" for p in report.problems]
    await sender.send(message.chat.id, "\n".join(lines))
