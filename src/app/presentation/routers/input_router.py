"""Ответ на вопрос кнопки: текст, которого кнопка собрать не может.

Роутер стоит между командами и text_router. Логика простая: если для этого чата
чего-то ждут — забираем реплику себе, иначе пропускаем дальше, в сессию.

Пропуск сделан через SkipHandler, а не через возврат из обработчика: в aiogram
обработчик, который вошёл в фильтр, считается сработавшим, и обычное сообщение
молча пропало бы, не дойдя до Claude. Это ровно тот случай, когда «ничего не
делать» надо делать явно.

Команды сюда не попадают намеренно: если человек, вместо ответа на вопрос,
набрал /status, значит он передумал. Ожидание снимается, команда работает как
обычно.
"""
from __future__ import annotations

import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import Message
from dishka.integrations.aiogram import FromDishka

from ...domain.enums import ReminderKind
from ...services.when import WhenParser
from ...infrastructure.telegram import ui
from ...infrastructure.telegram.formatting import esc
from ...infrastructure.telegram.sender import MessageSender
from ...services.conversation import ConversationService
from ...services.goals import GoalService
from ...services.journal import JournalService
from ...services.pending import PendingInput
from ...services.proactive import ProactiveService
from ...services.terms import TermsService
from ...settings import Settings

log = logging.getLogger(__name__)

router = Router(name="input")


@router.message(F.text)
async def on_answer(message: Message, pending: FromDishka[PendingInput],
                    journal: FromDishka[JournalService],
                    goals: FromDishka[GoalService],
                    proactive: FromDishka[ProactiveService],
                    parser: FromDishka[WhenParser],
                    terms: FromDishka[TermsService],
                    conversation: FromDishka[ConversationService],
                    settings: FromDishka[Settings],
                    sender: FromDishka[MessageSender]) -> None:
    text = (message.text or "").strip()
    chat_id = message.chat.id

    if text.startswith("/"):
        # Передумал: снимаем ожидание и отдаём команду её обработчику.
        if pending.cancel(chat_id):
            log.debug("ожидание снято командой %s", text.split()[0])
        raise SkipHandler

    action = pending.take(chat_id)
    if action is None:
        raise SkipHandler

    if action == "recall":
        await _recall(chat_id, text, journal, sender)
    elif action == "note":
        await _note(chat_id, text, journal, sender)
    elif action == "term":
        await _term(chat_id, text, terms, sender)
    elif action == "goal":
        await _goal(chat_id, text, goals, sender)
    elif action == "remind":
        await _remind(chat_id, text, proactive, parser, settings, sender)
    elif action == "cd":
        await _cd(chat_id, text, conversation, sender)
    else:
        # Ожидание пережило выкатку, в которой действие переименовали. Реплика
        # ценнее ожидания — отдаём её в сессию, а не теряем.
        log.warning("неизвестное ожидание %r, отдаю реплику в сессию", action)
        raise SkipHandler


# --------------------------------------------------------------------------- #
#  Обработчики отдельных ожиданий
# --------------------------------------------------------------------------- #
# Каждый получает то же, что и одноимённая команда, и отвечает тем же экраном —
# иначе кнопочный путь и командный разошлись бы в первый же день.
async def _recall(chat_id: int, text: str, journal: JournalService,
                  sender: MessageSender) -> None:
    entries = await journal.recall(text, 8)
    if not entries:
        await sender.send(chat_id, f"Ничего не нашёл по «{esc(text)}».",
                          reply_markup=ui.keyboard([ui.button("Память", "ui:memory")]))
        return
    lines = [ui.title("Нашёл в истории"), f"<i>{esc(text)}</i>", ""]
    for entry in entries:
        lines.append(f"<i>{entry.when.astimezone():%d.%m %H:%M}</i>  "
                     f"<b>{esc(entry.prompt[:110])}</b>")
        lines.append(f"{esc(entry.passage or entry.answer_head)}\n")
    await sender.send(chat_id, "\n".join(lines), reply_markup=ui.keyboard(
        [ui.button("Искать ещё", "ask:recall", ui.PRIMARY),
         ui.button("Память", "ui:memory")]))


async def _note(chat_id: int, text: str, journal: JournalService,
                sender: MessageSender) -> None:
    note = await journal.add_note(text, source="user")
    await sender.send(chat_id, f"{ui.title(f'Заметка #{note.id}')}\n\n{esc(note.text)}",
                      reply_markup=ui.keyboard(
                          [ui.button("Заметки", "ui:notes"),
                           ui.button("Память", "ui:memory")]))


async def _term(chat_id: int, text: str, terms: TermsService,
                sender: MessageSender) -> None:
    if "=" not in text:
        await sender.send(chat_id,
                          "Нужен знак равенства: <code>pgvector = пеговектор, пего вектор</code>",
                          reply_markup=ui.keyboard([ui.button("Ещё раз", "ask:term")]))
        return
    canonical, _, raw = text.partition("=")
    variants = [v.strip() for v in raw.split(",") if v.strip()]
    if not canonical.strip() or not variants:
        await sender.send(chat_id, "Нужно и слово, и хотя бы один вариант справа от «=».",
                          reply_markup=ui.keyboard([ui.button("Ещё раз", "ask:term")]))
        return
    await terms.learn(canonical.strip(), variants, source="user")
    await sender.send(chat_id, await _terms_text(terms), reply_markup=ui.keyboard(
        [ui.button("Ещё слово", "ask:term", ui.PRIMARY),
         ui.button("Словарь", "ui:terms")]))


async def _terms_text(terms: TermsService) -> str:
    mapping = await terms.mapping()
    return f"{ui.title('Выучил')}\n\n{ui.field('всего в словаре', str(len(mapping)))}"


async def _goal(chat_id: int, text: str, goals: GoalService,
                sender: MessageSender) -> None:
    body, _, criterion = text.partition("|")
    if not body.strip() or not criterion.strip():
        await sender.send(
            chat_id,
            "Нужны обе части через «|»: <i>что сделать | когда считать законченным</i>.\n"
            "Без признака завершения цель не кончается никогда.",
            reply_markup=ui.keyboard([ui.button("Ещё раз", "ask:goal")]))
        return
    goal = await goals.create(chat_id, body.strip(), criterion.strip())
    await sender.send(chat_id, "\n".join([
        ui.title(f"Цель #{goal.id} принята"), "",
        ui.field("что", goal.text), ui.field("готово, когда", goal.done_when),
    ]), reply_markup=ui.keyboard([ui.button("Цели", "ui:goals", ui.PRIMARY),
                                  ui.button("Меню", "ui:menu")]))


async def _remind(chat_id: int, text: str, proactive: ProactiveService,
                  parser: WhenParser, settings: Settings,
                  sender: MessageSender) -> None:
    schedule = await parser.parse(text)
    if schedule is None:
        await sender.send(
            chat_id, "Не понял, когда именно. Скажи точнее — «через час», «завтра в 10».",
            reply_markup=ui.keyboard([ui.button("Ещё раз", "ask:remind")]))
        return
    kind = (ReminderKind.COMMITMENT
            if schedule.repeat and schedule.repeat.startswith("times:")
            else ReminderKind.REMINDER)
    reminder = await proactive.schedule(chat_id=chat_id, text=schedule.text or text,
                                        at=schedule.at, repeat=schedule.repeat, kind=kind)
    when = schedule.at.astimezone(settings.tz).strftime("%d.%m в %H:%M")
    await sender.send(chat_id, "\n".join([
        ui.title(f"Напоминание #{reminder.id}"), "",
        ui.field("что", reminder.text), ui.field("когда", when),
    ]), reply_markup=ui.keyboard([ui.button("Что висит", "ui:todo", ui.PRIMARY),
                                  ui.button("Меню", "ui:menu")]))


async def _cd(chat_id: int, text: str, conversation: ConversationService,
              sender: MessageSender) -> None:
    target = Path(text).expanduser()
    if not target.is_dir():
        await sender.send(chat_id, f"Не директория: <code>{esc(str(target))}</code>",
                          reply_markup=ui.keyboard([ui.button("Ещё раз", "ask:cd")]))
        return
    await conversation.change_cwd(target)
    await sender.send(chat_id, f"{ui.title('Директория сменилась')}\n\n"
                               f"<code>{esc(str(target))}</code>\n"
                               f"<i>сессия перезапущена, контекст сохранён</i>",
                      reply_markup=ui.keyboard([ui.button("Сессия", "ui:session", ui.PRIMARY),
                                                ui.button("Меню", "ui:menu")]))
