"""Доменные модели (DM) — то, о чём система думает.

Правила этого файла:
  • только dataclass'ы и стандартная библиотека, никаких asyncpg/aiogram;
  • модель описывает суть, а не способ хранения: у Deployment нет ни SQL, ни
    JSON — превращение в строку таблицы живёт в репозитории;
  • модель может содержать поведение, если оно про саму суть (см. duration).

Отличие от DTO (domain/dto.py): DM — это то, что живёт в системе и хранится;
DTO — то, что летает между слоями на входе и выходе сервисов. Смешивать их
вредно: тогда любое изменение формы запроса тянет за собой изменение хранилища.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .enums import (DeployPhase, GoalStatus, ReminderKind, ReminderStatus,
                    TriggerKind, TurnSource, TurnStatus)


@dataclass(slots=True)
class Deployment:
    """Одна попытка самодопила: от коммита до вердикта сторожа."""

    note: str
    prev_sha: str
    new_sha: str
    phase: DeployPhase = DeployPhase.PENDING
    trigger: TriggerKind = TriggerKind.ASSISTANT
    files: list[str] = field(default_factory=list)
    reason: str | None = None          # почему откатились, если откатились
    chat_id: int | None = None         # куда сторожу писать о результате
    started_at: datetime | None = None
    finished_at: datetime | None = None
    revived_in_s: float | None = None  # сколько секунд бот поднимался
    id: int | None = None

    @property
    def duration_s(self) -> float | None:
        if not (self.started_at and self.finished_at):
            return None
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def succeeded(self) -> bool:
        return self.phase is DeployPhase.OK


@dataclass(slots=True)
class Goal:
    """Цель, к которой бот идёт сам.

    Три поля здесь важнее остальных. done_when — признак завершения: без него
    агент считает прогрессом любое шевеление и не останавливается никогда.
    budget_usd и max_turns_day — потолки: автономность без них означает, что
    счётчик крутится, пока человек спит.
    """

    chat_id: int
    text: str
    done_when: str
    status: GoalStatus = GoalStatus.ACTIVE
    cadence_min: int = 60
    budget_usd: float = 3.0
    spent_usd: float = 0.0
    max_turns_day: int = 8
    turns_today: int = 0
    turns_day: object = None
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    steps_done: int = 0
    last_note: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None
    id: int | None = None

    @property
    def budget_left(self) -> float:
        return max(0.0, self.budget_usd - self.spent_usd)

    @property
    def exhausted(self) -> bool:
        """Деньги кончились — цель останавливается сама, без напоминаний."""
        return self.spent_usd >= self.budget_usd


@dataclass(slots=True)
class GoalStep:
    """Один заход к цели: что сделано и во что обошлось."""

    goal_id: int
    summary: str
    cost_usd: float = 0.0
    turn_id: int | None = None
    created_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class Turn:
    """Один ход разговора: вопрос человека и что из этого вышло.

    Пишется в БД после завершения хода. Нужен не для отчётности, а чтобы я мог
    искать по собственной истории («когда я трогал сторожа и зачем») — этим
    занимается JournalService.
    """

    chat_id: int
    user_id: int | None
    prompt: str
    answer: str = ""
    status: TurnStatus = TurnStatus.OK
    source: TurnSource = TurnSource.TEXT
    tools: list[str] = field(default_factory=list)
    session_id: str | None = None
    goal_id: int | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    created_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class Note:
    """Заметка «на будущее себе»: решение, договорённость, грабли.

    Хранит и текст, и вектор. Вектор пока не заполняется (нет ключа к модели
    эмбеддингов), поиск идёт по триграммам и полнотексту — но колонка и индекс
    уже готовы, чтобы включение семантики не требовало миграции данных.
    """

    text: str
    tags: list[str] = field(default_factory=list)
    source: str = "assistant"
    created_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class Heartbeat:
    """Пульс процесса: то, по чему сторож снаружи понимает, что бот жив.

    Живёт в файле run/heartbeat.json, а не в БД, и это принципиально: сторож
    обязан работать, даже если Postgres лежит или бот не смог до него достучаться.
    """

    pid: int
    ts: float
    ready: bool
    commit: str | None = None
    session_id: str | None = None
    telegram_ok: bool = False
    claude_alive: bool = False

    @property
    def fully_healthy(self) -> bool:
        """Строгая проверка: мало запуститься — надо и Telegram видеть, и claude держать."""
        return self.ready and self.telegram_ok and self.claude_alive


@dataclass(slots=True)
class SttTerm:
    """Строчка живого словаря: как услышано → как должно быть.

    Пополняется прямо в разговоре: увидел кривое слово в расшифровке — добавил.
    Поэтому у записи есть source: видно, что пришло из затравки в коде, что
    добавил я сам, а что попросил человек.
    """

    canonical: str
    variant: str
    source: str = "assistant"
    created_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class Reminder:
    """Обязательство или напоминание — то, из-за чего бот заговорит первым.

    urgency растёт с каждым проигнорированным срабатыванием. Смысл не в том,
    чтобы кричать громче, а в том, чтобы менять формулировку: третье одинаковое
    «не забудь» человек уже не читает, а вопрос «это ещё актуально или снять?»
    заставляет принять решение.
    """

    chat_id: int
    text: str
    due_at: datetime
    kind: ReminderKind = ReminderKind.REMINDER
    status: ReminderStatus = ReminderStatus.OPEN
    repeat_rule: str | None = None
    urgency: int = 1
    fired_count: int = 0
    last_fired_at: datetime | None = None
    source: str = "user"
    source_turn_id: int | None = None
    created_at: datetime | None = None
    done_at: datetime | None = None
    id: int | None = None

    @property
    def overdue_for(self) -> float:
        """На сколько секунд просрочено. Отрицательное — ещё не время."""
        from datetime import datetime as _dt
        return (_dt.now().astimezone() - self.due_at).total_seconds()

    @property
    def nagging(self) -> bool:
        """Пора не напоминать, а спрашивать, актуально ли это вообще."""
        return self.fired_count >= 3


@dataclass(slots=True)
class Fact:
    """Устойчивое утверждение — то, что осталось после разговора.

    Ключевое отличие от заметки: у факта есть тождество (subject + key) и срок
    действия. Поэтому «таймзона» не накапливается пятью противоречащими копиями,
    а имеет одну действующую версию и цепочку закрытых предыдущих.

    pinned — это не «важно», а «нужно всегда». Профиль читается перед каждой
    серьёзной работой, и всё, что туда попало, отнимает место у остального.
    """

    value: str
    subject: str = "user"
    key: str | None = None
    kind: str = "fact"                 # fact | constant | preference | rule
    pinned: bool = False
    confidence: float = 1.0
    tags: list[str] = field(default_factory=list)
    source: str = "assistant"          # user — сказано прямо, assistant — выведено
    source_turn_id: int | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    superseded_by: int | None = None
    created_at: datetime | None = None
    id: int | None = None

    @property
    def active(self) -> bool:
        return self.valid_until is None

    @property
    def label(self) -> str:
        """Как факт читается человеком и мной: «ключ: значение»."""
        return f"{self.key}: {self.value}" if self.key else self.value


@dataclass(slots=True)
class Event:
    """То, что случилось или случится, — воспоминание с датой.

    date_precision честно хранит, насколько точно известно время. Без него
    «где-то в марте» превращается при выводе в «1 марта 00:00», и через полгода
    я буду уверенно врать человеку о его собственной жизни.
    """

    title: str
    happened_at: datetime
    details: str | None = None
    date_precision: str = "minute"     # minute | day | month | year
    kind: str = "event"                # event | decision | deadline | meeting | payment | trip
    people: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source: str = "assistant"
    source_turn_id: int | None = None
    created_at: datetime | None = None
    id: int | None = None

    def when_text(self, tz=None) -> str:
        """Дата словами ровно с той точностью, с какой она известна.

        tz обязателен на практике: Postgres отдаёт timestamptz в UTC, и без
        перевода в пояс человека «1 сентября» превращается в «31 августа» —
        ровно на те три часа, на которые Москва впереди.
        """
        moment = self.happened_at.astimezone(tz) if tz else self.happened_at
        if self.date_precision == "year":
            return moment.strftime("%Y")
        if self.date_precision == "month":
            return moment.strftime("%m.%Y")
        if self.date_precision == "day":
            return moment.strftime("%d.%m.%Y")
        return moment.strftime("%d.%m.%Y %H:%M")


@dataclass(slots=True)
class Chunk:
    """Кусок записи — то, по чему на самом деле ищется память.

    Ход разговора целиком слишком крупен для поиска: у длинного ответа один
    усреднённый вектор, не похожий ни на одну из его мыслей. Кусок — это одна
    законченная мысль со своим вектором, знающая, из какой записи она взята.
    """

    source_kind: str          # turn | note
    source_id: int
    ord: int
    role: str                 # user | assistant
    text: str
    created_at: datetime
    id: int | None = None
