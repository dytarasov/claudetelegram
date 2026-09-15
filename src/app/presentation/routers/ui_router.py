"""Кнопки: перерисовка экранов на месте, без новых сообщений.

Главный приём — редактирование того же сообщения. Пять нажатий «Обновить» не
должны оставлять пять сообщений в чате: экран живёт на одном месте и меняется
под пальцем. Это и есть разница между ботом с кнопками и ботом со списком
команд.

Необратимые действия проходят через подтверждение: кнопка сначала меняет экран
на вопрос, и только вторая кнопка выполняет.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, ForceReply
from dishka.integrations.aiogram import FromDishka

from ...domain.dto import RollbackCommand, TurnRequest
from ...domain.enums import TriggerKind, TurnSource
from ...domain.ports import SpeechToText
from ...infrastructure.telegram import ui
from ...infrastructure.telegram.formatting import esc
from ...infrastructure.telegram.sender import MessageSender
from ...services.conversation import ConversationService
from ...services.goals import GoalService
from ...services.journal import JournalService
from ...services.memory import MemoryService
from ...services.pending import PendingInput
from ...services.health import HealthService
from ...services.logs import LogService
from ...services.proactive import ProactiveService
from ...services.selfupdate import SelfUpdateService
from ...services.terms import TermsService
from ...settings import Settings
from .. import screens

router = Router(name="ui")


async def _render(callback: CallbackQuery, screen: tuple[str, object],
                  notice: str = "") -> None:
    """Заменить текст и клавиатуру текущего сообщения."""
    text, keys = screen
    if callback.message is not None:
        try:
            await callback.message.edit_text(text, reply_markup=keys)
        except Exception:  # noqa: BLE001 — «ничего не изменилось» это не ошибка
            pass
    await callback.answer(notice)


@router.callback_query(F.data == "ui:dismiss")
async def on_dismiss(callback: CallbackQuery) -> None:
    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
    await callback.answer("отменено")


@router.callback_query(F.data == "ui:status")
async def on_status(callback: CallbackQuery, conversation: FromDishka[ConversationService],
                    health: FromDishka[HealthService], stt: FromDishka[SpeechToText],
                    settings: FromDishka[Settings]) -> None:
    await _render(callback, await screens.status(conversation, health, stt, settings))


@router.callback_query(F.data == "ui:diag")
async def on_diag(callback: CallbackQuery, health: FromDishka[HealthService],
                  selfupdate: FromDishka[SelfUpdateService]) -> None:
    await _render(callback, await screens.diagnostics(health, selfupdate))


@router.callback_query(F.data == "ui:versions")
async def on_versions(callback: CallbackQuery,
                      selfupdate: FromDishka[SelfUpdateService]) -> None:
    await _render(callback, await screens.versions(selfupdate))


@router.callback_query(F.data == "ui:todo")
async def on_todo(callback: CallbackQuery, proactive: FromDishka[ProactiveService],
                  settings: FromDishka[Settings]) -> None:
    await _render(callback, await screens.todo(proactive, callback.message.chat.id, settings))


@router.callback_query(F.data.startswith("ui:logs:"))
async def on_logs(callback: CallbackQuery, logs: FromDishka[LogService]) -> None:
    limit = int((callback.data or "ui:logs:30").rsplit(":", 1)[-1])
    await _render(callback, await screens.logs(logs, "events", limit))


@router.callback_query(F.data == "ui:errors")
async def on_errors(callback: CallbackQuery, logs: FromDishka[LogService]) -> None:
    lines = logs.problems(30)
    text = "\n".join([ui.title("Тревожное в логе"), f"<i>последние {len(lines)}</i>", "",
                      ui.mono_block("\n".join(lines) if lines else "чисто")])
    keys = ui.keyboard([ui.button("Обновить", "ui:errors", ui.PRIMARY),
                        ui.button("Весь лог", "ui:logs:30")],
                       [ui.button("Состояние", "ui:status"),
                        ui.button("Диагностика", "ui:diag")])
    await _render(callback, (text, keys))


@router.callback_query(F.data == "ui:logfile")
async def on_logfile(callback: CallbackQuery, logs: FromDishka[LogService],
                     sender: FromDishka[MessageSender]) -> None:
    try:
        path, compressed = logs.export("events")
    except Exception as exc:  # noqa: BLE001
        await callback.answer(str(exc)[:180], show_alert=True)
        return
    await sender.send_file(callback.message.chat.id, path)
    await callback.answer("прислал файлом" + (" (сжат)" if compressed else ""))


@router.callback_query(F.data == "ui:stop")
async def on_stop(callback: CallbackQuery,
                  conversation: FromDishka[ConversationService]) -> None:
    if not conversation.busy:
        await callback.answer("ход не идёт")
        return
    await callback.answer("прерываю…")
    if not await conversation.interrupt():
        await conversation.restart()


@router.callback_query(F.data == "ui:queue_clear")
async def on_queue_clear(callback: CallbackQuery,
                         conversation: FromDishka[ConversationService]) -> None:
    """Снять всё, что ждёт в очереди. Активный ход не трогаем — для него ui:stop.

    Уведомление «в очереди: N» после нажатия перерисовываем в итог, чтобы кнопка
    не осталась висеть на уже пустой очереди и её нельзя было нажать повторно."""
    dropped = conversation.clear_queue()
    if callback.message is not None:
        text = (f"<i>очередь очищена, снято: {dropped}</i>" if dropped
                else "<i>очередь уже пуста</i>")
        try:
            await callback.message.edit_text(text)
        except Exception:  # noqa: BLE001 — «ничего не изменилось» это не ошибка
            pass
    await callback.answer("очередь очищена" if dropped else "очередь пуста")


@router.callback_query(F.data.startswith("ui:rollback_ask:"))
async def on_rollback_ask(callback: CallbackQuery,
                          selfupdate: FromDishka[SelfUpdateService]) -> None:
    target = (callback.data or "").rsplit(":", 1)[-1]
    subject = selfupdate._git.subject(target)  # noqa: SLF001 — только для показа
    text = "\n".join([
        ui.title("Откатиться?"),
        "",
        ui.field("версия", target, mono=True),
        ui.field("что в ней", subject),
        "",
        "<i>Процесс перезапустится, контекст разговора сохранится. "
        "Текущая версия не пропадёт — останется под меткой.</i>",
    ])
    await _render(callback, (text, ui.confirm("Откатить", f"ui:rollback:{target}")))


@router.callback_query(F.data.startswith("ui:rollback:"))
async def on_rollback(callback: CallbackQuery,
                      selfupdate: FromDishka[SelfUpdateService]) -> None:
    target = (callback.data or "").rsplit(":", 1)[-1]
    result = await selfupdate.rollback(RollbackCommand(
        target=target, chat_id=callback.message.chat.id, trigger=TriggerKind.USER,
        reason=f"откат кнопкой на {target}",
    ))
    head = "Откат запущен" if result.ok else "Откат не начался"
    text = f"{ui.title(head)}\n\n<i>{esc(result.message)}</i>"
    await _render(callback, (text, ui.keyboard([ui.button("Версии", "ui:versions")])),
                  "перезапускаюсь" if result.ok else "не вышло")


@router.callback_query(F.data == "ui:goals")
async def on_goals(callback: CallbackQuery, goals: FromDishka[GoalService]) -> None:
    await _render(callback, await screens.goals(goals, callback.message.chat.id))


@router.callback_query(F.data.startswith("goal:"))
async def on_goal_action(callback: CallbackQuery, goals: FromDishka[GoalService]) -> None:
    from ...domain.enums import GoalStatus

    _, action, raw_id = (callback.data or "").split(":")
    status = {"pause": GoalStatus.PAUSED, "resume": GoalStatus.ACTIVE,
              "stop": GoalStatus.STOPPED}[action]
    await goals.set_status(int(raw_id), status)
    await _render(callback, await screens.goals(goals, callback.message.chat.id),
                  {"pause": "на паузе", "resume": "в работе", "stop": "снята"}[action])


# --------------------------------------------------------------------------- #
#  Корневое меню и разделы
# --------------------------------------------------------------------------- #
async def _to_session(callback: CallbackQuery, conversation: ConversationService,
                      text: str) -> None:
    """Отправить реплику в сессию от имени нажавшего кнопку.

    Кнопка — такой же источник хода, как набранное сообщение, поэтому запрос
    собирается тот же самый. reply_to не ставим: отвечать на собственный экран
    бота бессмысленно, ответ придёт отдельным сообщением.
    """
    await conversation.enqueue(TurnRequest(
        chat_id=callback.message.chat.id,
        user_id=callback.from_user.id if callback.from_user else None,
        text=text,
        source=TurnSource.TEXT,
    ))


@router.callback_query(F.data == "ui:menu")
async def on_menu(callback: CallbackQuery, conversation: FromDishka[ConversationService],
                  health: FromDishka[HealthService]) -> None:
    await _render(callback, await screens.menu(conversation, health))


@router.callback_query(F.data == "ui:session")
async def on_session(callback: CallbackQuery, conversation: FromDishka[ConversationService],
                     settings: FromDishka[Settings]) -> None:
    await _render(callback, await screens.session(conversation, settings))


@router.callback_query(F.data == "ui:models")
async def on_models(callback: CallbackQuery,
                    conversation: FromDishka[ConversationService]) -> None:
    await _render(callback, screens.models(conversation.process.status()["model"]))


@router.callback_query(F.data.startswith("ui:model:"))
async def on_model_pick(callback: CallbackQuery,
                        conversation: FromDishka[ConversationService],
                        settings: FromDishka[Settings]) -> None:
    name = (callback.data or "").split(":", 2)[-1]
    await conversation.change_model(name)
    await _to_session(callback, conversation, f"/model {name}")
    await _render(callback, await screens.session(conversation, settings), "модель сменил")


@router.callback_query(F.data == "ui:ctx")
async def on_ctx(callback: CallbackQuery,
                 conversation: FromDishka[ConversationService]) -> None:
    await _to_session(callback, conversation, "/context")
    await callback.answer("спрашиваю расход")


@router.callback_query(F.data == "ui:compact")
async def on_compact(callback: CallbackQuery,
                     conversation: FromDishka[ConversationService]) -> None:
    await _to_session(callback, conversation, "/compact")
    await callback.answer("сворачиваю контекст")


@router.callback_query(F.data == "ui:restart_ask")
async def on_restart_ask(callback: CallbackQuery) -> None:
    text = "\n".join([
        ui.title("Перезапустить процесс?"),
        "", "<i>Контекст разговора сохранится — сессия поднимется на том же id. "
            "Незаконченный ход прервётся.</i>",
    ])
    await _render(callback, (text, ui.confirm("Перезапустить", "ui:restart", ui.DANGER)))


@router.callback_query(F.data == "ui:restart")
async def on_restart(callback: CallbackQuery, conversation: FromDishka[ConversationService],
                     settings: FromDishka[Settings]) -> None:
    await conversation.restart()
    await _render(callback, await screens.session(conversation, settings), "перезапустил")


@router.callback_query(F.data == "ui:new_ask")
async def on_new_ask(callback: CallbackQuery) -> None:
    text = "\n".join([
        ui.title("Начать сессию заново?"),
        "", "<b>Контекст будет потерян.</b> Журнал и память останутся: всё, что "
            "записано, найдётся поиском. Но нить разговора оборвётся.",
    ])
    await _render(callback, (text, ui.confirm("Начать заново", "ui:new", ui.DANGER)))


@router.callback_query(F.data == "ui:new")
async def on_new(callback: CallbackQuery, conversation: FromDishka[ConversationService],
                 settings: FromDishka[Settings]) -> None:
    await conversation.restart(fresh=True)
    await _render(callback, await screens.session(conversation, settings), "сессия чистая")


@router.callback_query(F.data == "ui:code")
async def on_code(callback: CallbackQuery, health: FromDishka[HealthService],
                  selfupdate: FromDishka[SelfUpdateService]) -> None:
    await _render(callback, await screens.code(health, selfupdate))


@router.callback_query(F.data == "ui:stable_ask")
async def on_stable_ask(callback: CallbackQuery) -> None:
    text = "\n".join([
        ui.title("Закрепить текущую версию?"),
        "", "<i>Сторож будет откатывать сюда, если следующая выкатка не оживёт.</i>",
    ])
    await _render(callback, (text, ui.confirm("Закрепить", "ui:stable", ui.SUCCESS)))


@router.callback_query(F.data == "ui:stable")
async def on_stable(callback: CallbackQuery, health: FromDishka[HealthService],
                    selfupdate: FromDishka[SelfUpdateService]) -> None:
    sha = selfupdate.mark_stable("HEAD")
    await _render(callback, await screens.code(health, selfupdate),
                  f"stable → {sha[:7]}")


# ---- память ---------------------------------------------------------------- #
@router.callback_query(F.data == "ui:memory")
async def on_memory(callback: CallbackQuery, memory: FromDishka[MemoryService],
                    journal: FromDishka[JournalService]) -> None:
    await _render(callback, await screens.memory(memory, journal))


@router.callback_query(F.data == "ui:profile")
async def on_profile(callback: CallbackQuery, memory: FromDishka[MemoryService]) -> None:
    await _render(callback, await screens.profile(memory))


@router.callback_query(F.data.startswith("ui:timeline"))
async def on_timeline(callback: CallbackQuery, memory: FromDishka[MemoryService]) -> None:
    parts = (callback.data or "").split(":")
    back, ahead = (int(parts[2]), int(parts[3])) if len(parts) == 4 else (7, 14)
    await _render(callback, await screens.timeline_screen(memory, back, ahead))


@router.callback_query(F.data == "ui:notes")
async def on_notes(callback: CallbackQuery, journal: FromDishka[JournalService]) -> None:
    await _render(callback, await screens.notes_screen(journal))


@router.callback_query(F.data == "ui:terms")
async def on_terms(callback: CallbackQuery, terms: FromDishka[TermsService]) -> None:
    await _render(callback, await screens.terms_screen(terms))


# ---- запрос текста --------------------------------------------------------- #
# Кнопка не умеет собрать произвольный текст, поэтому она запоминает, чего мы
# ждём, и просит ответить. Следующая реплика уйдёт в input_router, а не в сессию.
ASK_PROMPTS = {
    "recall": ("Что вспомнить?", "своими словами: о чём был разговор"),
    "note": ("Что записать в заметки?", "вывод, грабли, решение"),
    "term": ("Чему научить словарь?", "pgvector = пеговектор, пего вектор"),
    "goal": ("Какая цель?", "что сделать | когда считать законченным"),
    "remind": ("О чём напомнить?", "завтра в 10 позвонить бате"),
    "cd": ("Новая рабочая директория?", "/root/tgassistant"),
}


@router.callback_query(F.data.startswith("ask:"))
async def on_ask(callback: CallbackQuery, pending: FromDishka[PendingInput],
                 sender: FromDishka[MessageSender]) -> None:
    action = (callback.data or "").split(":", 1)[-1]
    prompt = ASK_PROMPTS.get(action)
    if prompt is None:
        await callback.answer("не знаю такого действия")
        return
    title, hint = prompt
    pending.expect(callback.message.chat.id, action)
    await sender.send(
        callback.message.chat.id,
        f"{ui.title(title)}\n<i>например: {esc(hint)}</i>",
        reply_markup=ForceReply(input_field_placeholder=hint[:64]),
    )
    await callback.answer("жду ответа")
