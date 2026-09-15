"""Разговор: очередь ходов, проведение хода, журнал, восстановление после смерти.

Это сердце приложения и единственное место, где принимаются решения о ходе:
  • ходы идут строго по одному — иначе два ответа перемешались бы в одном
    процессе claude, у которого один stdin на всех;
  • если процесс умер посреди хода, мы не теряем разговор: поднимаем его заново
    с тем же id сессии и говорим человеку повторить;
  • каждый завершённый ход уходит в журнал (таблица turns) — это моя память о
    том, что здесь было, и по ней потом работает /recall.

Сервис не знает ни про aiogram, ни про SQL: он работает с портами и DTO.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime
from pathlib import Path

from ..domain.dto import TurnOutcome, TurnRequest
from ..domain.enums import TurnSource, TurnStatus
from ..domain.errors import SessionDead
from ..domain.models import Turn
from ..domain.ports import (AssistantProcess, KeyValueRepository, Notifier,
                           TurnPresenter, TurnPresenterFactory, TurnRepository)
from ..settings import Settings
from .terms import TermsService

from . import events

log = logging.getLogger(__name__)

# Приписка к наговоренным репликам.
#
# Здесь два разных механизма, и это намеренно. Словарь (services/terms.py) чинит
# известные слова детерминированно — он быстрый, предсказуемый и не тратит токены.
# Приписка страхует от всего остального: модель узнаёт, что перед ней расшифровка,
# видит, что именно словарь уже подменил (и может это отменить, если подмена
# оказалась неуместной), и знает, как пополнить словарь новым словом.
#
# Напечатанный текст ничего этого не получает: текст — это текст, портить его
# служебной обвязкой незачем.
VOICE_PREAMBLE = (
    "[Голосовое сообщение, расшифровано автоматически. Термины, имена и команды "
    "могли быть услышаны неточно: понимай по смыслу, явные огрехи исправляй молча, "
    "а если из-за них теряется суть просьбы — переспроси, не догадывайся."
)
# Заголовок времени к каждой реплике.
#
# Модель внутри разговора не знает, который час и сколько прошло с прошлой
# фразы: для неё вся переписка — один непрерывный текст. Без этого «вчера»,
# «на прошлой неделе» и «мы это делали позавчера» не с чем соотнести, а при
# ежедневном общении именно это и требуется чаще всего. Тридцать токенов на ход
# — цена, за которую время перестаёт быть догадкой.
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря")

VOICE_HINT_ADD = (
    " Заметишь устойчиво перевранное слово — запомни его сам: "
    "venv/bin/python src/app/cli.py term add <как-должно-быть> <как-услышалось>."
)


class ConversationService:
    def __init__(
        self,
        process: AssistantProcess,
        notifier: Notifier,
        presenters: TurnPresenterFactory,
        turns: TurnRepository,
        kv: KeyValueRepository,
        terms: TermsService,
        settings: Settings,
    ) -> None:
        self._process = process
        self._notifier = notifier
        self._presenters = presenters
        self._turns = turns
        self._kv = kv
        self._terms = terms
        self._s = settings

        self._queue: asyncio.Queue[TurnRequest] = asyncio.Queue()
        self._active: TurnRequest | None = None
        self._started_at = 0.0
        self._last_turn_at: datetime | None = None
        # Ставится в main: цель узнаёт о результате своего хода отсюда.
        # Через атрибут, а не зависимость — иначе разговор и цели ссылались бы
        # друг на друга и контейнер не собрался бы.
        self.on_goal_turn = None

    # ---- состояние ------------------------------------------------------- #
    @property
    def busy(self) -> bool:
        return self._active is not None

    @property
    def queued(self) -> int:
        return self._queue.qsize()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started_at if self._active else 0.0

    @property
    def process(self) -> AssistantProcess:
        """Роутерам иногда нужен прямой доступ (статус, смена модели)."""
        return self._process

    # ---- очередь --------------------------------------------------------- #
    async def enqueue(self, request: TurnRequest) -> int:
        """Поставить ход в очередь. Возвращает, сколько теперь ждёт."""
        if not request.text.strip():
            return self.queued
        await self._queue.put(request)
        events.turn_queued(str(request.source), len(request.text), self._queue.qsize())
        return self._queue.qsize()

    def clear_queue(self) -> int:
        """Выбросить всё, что ждёт в очереди. Возвращает, сколько сняли.

        Активный ход не трогаем — он останавливается отдельно, через interrupt():
        это два разных действия и две разных кнопки. asyncio.Queue не умеет
        очищаться разом, поэтому вычерпываем вручную, помечая каждый вынутый
        элемент task_done(), чтобы внутренний счётчик очереди остался согласован.
        """
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()
            dropped += 1
        if dropped:
            events.queue_cleared(dropped)
        return dropped

    async def run_worker(self) -> None:
        """Вечный цикл обработки очереди. Запускается один раз при старте."""
        while True:
            request = await self._queue.get()
            try:
                await self._run_turn(request)
            except Exception:  # noqa: BLE001 — воркер обязан пережить любой ход
                log.exception("ход упал непойманным исключением")
            finally:
                self._queue.task_done()

    # ---- один ход -------------------------------------------------------- #
    async def _typing(self, chat_id: int) -> None:
        """Telegram гасит «печатает…» через пять секунд — поэтому цикл."""
        while True:
            await self._notifier.typing(chat_id)
            await asyncio.sleep(4)

    def _time_header(self) -> str:
        """Когда сейчас и сколько прошло с прошлой реплики."""
        now = datetime.now().astimezone()
        stamp = (f"{WEEKDAYS[now.weekday()]}, {now.day} {MONTHS[now.month - 1]} "
                 f"{now.year}, {now:%H:%M %Z}")
        gap = ""
        if self._last_turn_at is not None:
            minutes = (now - self._last_turn_at).total_seconds() / 60
            if minutes < 60:
                gap = f" · с прошлой реплики {minutes:.0f} мин"
            elif minutes < 60 * 24:
                gap = f" · с прошлой реплики {minutes / 60:.0f} ч"
            else:
                gap = f" · прошлых реплик не было {minutes / 60 / 24:.0f} дн"
        return f"[сейчас {stamp}{gap}]"

    async def _prompt_for_model(self, request: TurnRequest) -> str:
        """Текст, который реально уходит в claude.

        Напечатанное уходит слово в слово. Наговоренное — сначала через словарь,
        потом с припиской: что это расшифровка, что именно словарь подменил и как
        добавить в него новое слово. В журнал при этом пишется исходная реплика
        без служебной обвязки: искать потом хочется по словам человека.
        """
        # Автономный ход уже несёт всё нужное внутри задания, включая время:
        # добавлять шапку значило бы дублировать её.
        if request.source is TurnSource.AGENT:
            return request.text

        # Команды самого Claude Code (/compact, /context, /model) обязаны стоять
        # в начале сообщения — иначе они не распознаются и уходят модели простым
        # текстом. Именно это и сломала отметка времени: /compact переставал
        # сворачивать контекст и просто оказывался репликой в разговоре.
        if request.text.lstrip().startswith("/"):
            return request.text

        header = self._time_header()
        if request.source is not TurnSource.VOICE:
            return f"{header}\n\n{request.text}"

        text, applied = await self._terms.normalize(request.text)
        if applied:
            events.terms_applied(len(applied), "; ".join(str(r) for r in applied[:4]))
        preamble = VOICE_PREAMBLE
        if applied:
            preamble += (" По словарю уже поправлено: "
                         + "; ".join(str(r) for r in applied)
                         + " — если подмена неуместна, читай исходное слово.")
        return f"{header}\n{preamble}{VOICE_HINT_ADD}]\n\n{text}"

    async def _run_turn(self, request: TurnRequest) -> TurnOutcome:
        self._active = request
        self._started_at = time.monotonic()
        if request.note:
            await self._notifier.send(request.chat_id, request.note)

        events.turn_started(str(request.source), request.text.strip()[:80].replace("\n", " "))
        view: TurnPresenter = self._presenters.create(request.chat_id, request.reply_to)
        typing = asyncio.create_task(self._typing(request.chat_id))
        started = time.monotonic()
        outcome = TurnOutcome(ok=False, status=TurnStatus.FAILED)
        try:
            await view.start()
            prompt = await self._prompt_for_model(request)
            result = await self._process.ask(prompt, view.on_event)
            await view.finish(result)
            usage = (result.get("usage") or {})
            outcome = TurnOutcome(
                ok=not result.get("is_error"),
                status=(TurnStatus.INTERRUPTED
                        if result.get("subtype") == "error_during_execution"
                        else TurnStatus.OK),
                answer=view.answer,
                tools=view.tools,
                tokens_in=int(usage.get("input_tokens") or 0),
                tokens_out=int(usage.get("output_tokens") or 0),
                cost_usd=float(result.get("total_cost_usd") or 0.0),
                duration_s=time.monotonic() - started,
            )
            await self._persist_state()
            events.turn_finished(str(outcome.status), outcome.duration_s, len(outcome.tools),
                                 outcome.tokens_in + outcome.tokens_out, outcome.cost_usd)
        except SessionDead as exc:
            view.close()
            outcome = TurnOutcome(ok=False, status=TurnStatus.SESSION_DEAD,
                                  error=str(exc), duration_s=time.monotonic() - started)
            events.claude_died(str(exc))
            await self._recover(request.chat_id, exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("ход упал")
            view.close()
            outcome = TurnOutcome(ok=False, status=TurnStatus.FAILED, error=str(exc),
                                  duration_s=time.monotonic() - started)
            events.turn_failed(str(TurnStatus.FAILED), str(exc))
            await self._notifier.send_error(request.chat_id, "<b>Ошибка хода</b>", str(exc))
        finally:
            typing.cancel()
            self._active = None
            self._last_turn_at = datetime.now().astimezone()
            await self._journal(request, view, outcome)
        return outcome

    async def _recover(self, chat_id: int, exc: Exception) -> None:
        """Процесс claude умер: поднимаем с тем же контекстом и честно говорим об этом."""
        await self._notifier.send_error(
            chat_id, "<b>Сессия Claude оборвалась</b>", str(exc))
        try:
            await self._process.restart()
            await self._notifier.send(chat_id, "<i>сессия восстановлена, повтори запрос</i>")
        except Exception as exc2:  # noqa: BLE001
            await self._notifier.send_error(chat_id, "<b>Не поднялась</b>", str(exc2))

    async def _journal(self, request: TurnRequest, view: TurnPresenter,
                       outcome: TurnOutcome) -> None:
        """Записать ход в журнал. Падение журнала не должно ломать разговор."""
        saved = None
        try:
            saved = await self._turns.add(Turn(
                chat_id=request.chat_id, user_id=request.user_id,
                prompt=request.text, answer=outcome.answer or view.answer,
                status=TurnStatus(outcome.status), source=request.source,
                tools=outcome.tools,
                session_id=self._process.status().get("session_id"),
                tokens_in=outcome.tokens_in, tokens_out=outcome.tokens_out,
                cost_usd=outcome.cost_usd, duration_s=outcome.duration_s,
                goal_id=request.goal_id,
            ))
        except Exception:  # noqa: BLE001
            log.warning("не смог записать ход в журнал", exc_info=True)

        # Отчитаться перед целью нужно даже если журнал не записался: без этого
        # цель не узнает ни о расходе, ни о том, что шаг вообще был сделан.
        if request.goal_id and self.on_goal_turn is not None:
            try:
                await self.on_goal_turn(request.goal_id, outcome,
                                        saved.id if saved else None)
            except Exception:  # noqa: BLE001
                log.warning("не смог отчитаться по цели %s", request.goal_id, exc_info=True)

    # ---- управление сессией ---------------------------------------------- #
    async def _persist_state(self) -> None:
        """Сохранить то, без чего перезапуск потеряет разговор."""
        status = self._process.status()
        for key in ("session_id", "cwd", "model"):
            value = status.get(key)
            if value:
                await self._kv.set(key, str(value))

    async def interrupt(self) -> bool:
        ok = await self._process.interrupt()
        if ok:
            events.turn_interrupted()
        return ok

    async def restart(self, *, fresh: bool = False) -> None:
        await self._process.restart(fresh=fresh)
        await self._persist_state()
        events.claude_restarted("с чистого листа" if fresh else "контекст сохранён",
                                self._process.status().get("session_id"))

    async def change_cwd(self, path: Path) -> None:
        """Директория меняется только вместе с перезапуском процесса: claude
        берёт её при старте и на лету не переучивается."""
        self._process.set_cwd(path)
        await self.restart()

    async def change_model(self, name: str) -> None:
        """Модель подхватится следующим ходом — перезапуск не нужен."""
        self._process.set_model(name)
