"""Точка входа: собрать контейнер, поднять бота, ровно закрыться.

Здесь нет ни одной бизнес-строчки — только порядок запуска:

  1. настройки и проверка, что бот вообще имеет право стартовать;
  2. контейнер (app/di.py) и получение долгоживущих объектов;
  3. диспетчер aiogram: сначала защита (AuthMiddleware), потом роутеры,
     потом dishka — чтобы хендлеры получали сервисы прямо в аргументах;
  4. фоновые задачи: обработчик очереди ходов и пульс;
  5. поллинг Telegram, а в finally — аккуратное закрытие всего.

Пульс на четвёртом шаге — то, по чему сторож снаружи поймёт, что новая версия
поднялась. Пока эта корутина крутится, откат не случится.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from dishka.integrations.aiogram import setup_dishka


from .di import build_container
from .domain.enums import DeployPhase
from .domain.ports import AssistantProcess, SpeechToText
from .infrastructure.telegram.formatting import esc
from .infrastructure.logs.setup import setup_logging
from .infrastructure.telegram.sender import MessageSender
from .presentation.middlewares.auth import AuthMiddleware
from .presentation.routers import build_root_router
from .services import events
from .services.conversation import ConversationService
from .services.health import HealthService
from .services.goals import GoalService
from .services.journal import JournalService
from .services.proactive import ProactiveService
from .services.selfupdate import SelfUpdateService
from .settings import Settings

log = logging.getLogger("app")

COMMANDS = [
    ("menu", "всё меню кнопками"),
    ("ctx", "расход контекста"),
    ("compact", "свернуть контекст"),
    ("status", "состояние сессии"),
    ("stop", "прервать ход"),
    ("new", "новая сессия"),
    ("cd", "сменить директорию"),
    ("model", "сменить модель"),
    ("get", "прислать файл с сервера"),
    ("restart", "перезапустить процесс"),
    ("versions", "версии кода"),
    ("rollback", "откатить на стабильную версию"),
    ("stable", "закрепить версию стабильной"),
    ("diag", "здоровье и последняя выкатка"),
    ("recall", "поиск по прошлым разговорам"),
    ("note", "записать заметку"),
    ("notes", "показать заметки"),
    ("memory", "состояние векторной памяти"),
    ("terms", "словарь расшифровки голосовых"),
    ("term", "научить словарь новому слову"),
    ("goal", "поставить цель"),
    ("goals", "цели в работе"),
    ("remind", "напомнить о чём-то"),
    ("todo", "что висит"),
    ("done", "закрыть напоминание"),
    ("logs", "последние события"),
    ("errors", "что шло не так"),
    ("logfile", "выгрузить лог файлом"),
    ("stats", "сколько ходов и денег"),
    ("help", "справка"),
]


async def announce_update(selfupdate: SelfUpdateService, sender: MessageSender) -> None:
    """Сказать в чат, что поднялись на новой версии.

    Сообщение отправляется один раз: если бот войдёт в цикл перезапусков, он не
    завалит чат — флаг announced лежит в том же файле, что читает сторож.
    """
    marker = selfupdate.read_marker()
    if not marker or marker.get("announced"):
        return
    if marker.get("phase") not in (DeployPhase.PENDING, DeployPhase.APPLYING,
                                   DeployPhase.SOAKING):
        return
    chat_id = marker.get("chat_id")
    if not chat_id:
        return
    selfupdate.mark_announced()

    files = marker.get("files") or []
    lines = [
        f"<b>Поднялся на новой версии</b> <code>{esc(str(marker.get('new'))[:7])}</code>",
        esc(str(marker.get("note") or "")),
    ]
    if files:
        lines.append(f"<i>{esc(', '.join(files[:8]))}</i>")
    lines += [
        "",
        "Сторож смотрит ещё пару минут: не взлетит — сам вернёт "
        f"<code>{esc(str(marker.get('prev'))[:7])}</code>.",
        "Что-то не так на глаз — /rollback",
    ]
    await sender.send(int(chat_id), "\n".join(lines))


async def index_memory(journal: JournalService, every_s: int) -> None:
    """Досчитывать векторы для новых записей — фоном и без спешки.

    Отдельной задачей, а не в цикле пульса: обращение к чужому API может
    подвиснуть на десятки секунд, а пульс обязан стучать ровно, иначе сторож
    решит, что бот умер, и откатит живую версию.
    """
    while True:
        try:
            await journal.index_pending()
        except Exception:  # noqa: BLE001 — фоновая работа не роняет бота
            log.warning("индексация памяти не удалась", exc_info=True)
        await asyncio.sleep(every_s)


async def run() -> None:
    container = build_container()
    settings = await container.get(Settings)

    # Логи настраиваем первым делом: всё, что случится дальше, должно попасть
    # и в journald, и в файлы (подробный лог и человеческая лента событий).
    setup_logging(settings)
    problems = settings.problems()
    if any("TELEGRAM_BOT_TOKEN" in p or "ALLOWED_USER_IDS" in p for p in problems):
        for p in problems:
            log.error(p)
        raise SystemExit(1)
    for p in problems:
        log.warning(p)

    bot = await container.get(Bot)
    sender = await container.get(MessageSender)
    process = await container.get(AssistantProcess)
    conversation = await container.get(ConversationService)
    health = await container.get(HealthService)
    selfupdate = await container.get(SelfUpdateService)
    stt = await container.get(SpeechToText)
    journal = await container.get(JournalService)
    proactive = await container.get(ProactiveService)
    goals = await container.get(GoalService)
    # Замыкаем круг: цели кладут задания в ту же очередь, что и реплики
    # человека, а разговор отчитывается перед целью о расходе каждого хода.
    goals.bind(conversation.enqueue)
    conversation.on_goal_turn = goals.record_turn

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    settings.run_dir.mkdir(parents=True, exist_ok=True)

    dp = Dispatcher()
    dp.update.outer_middleware(AuthMiddleware(settings.allowed_user_ids))
    dp.include_router(build_root_router())
    setup_dishka(container=container, router=dp, auto_inject=True)

    await process.start()
    status = process.status()
    log.info("сессия claude: %s", status.get("session_id"))
    events.started(health.snapshot().commit or "?", status.get("session_id"),
                   status.get("model") or settings.claude_model)

    tasks = [
        asyncio.create_task(conversation.run_worker(), name="turns"),
        # Проактивность: единственная задача, которая начинает разговор сама.
        asyncio.create_task(proactive.run_loop(), name="reminders"),
        asyncio.create_task(goals.run_loop(), name="goals"),
        # Пульс заодно переносит вердикт сторожа из файла в БД: сторож про
        # Postgres не знает и знать не должен.
        asyncio.create_task(
            health.run_loop(on_beat=selfupdate.sync_from_guard), name="heartbeat",
        ),
    ]
    # Локальная модель распознавания грузится с полминуты — лучше заранее.
    tasks.append(asyncio.create_task(stt.warm_up(), name="stt-warmup"))
    if journal.semantic_available:
        tasks.append(asyncio.create_task(
            index_memory(journal, settings.embeddings_index_interval_s), name="memory-index",
        ))
        log.info("семантическая память включена")

    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    await announce_update(selfupdate, sender)

    try:
        await dp.start_polling(bot, handle_signals=True)
    finally:
        events.stopping()
        for task in tasks:
            task.cancel()
        await process.stop()
        await container.close()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
