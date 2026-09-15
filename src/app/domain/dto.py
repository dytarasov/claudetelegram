"""DTO — то, что летает между слоями.

Зачем они отдельно от моделей: роутер не должен знать, как устроено хранилище,
а сервис не должен принимать aiogram-объекты. DTO — узкий контракт на границе:
роутер собрал DeployCommand из текста команды, сервис вернул DeployResult,
роутер превратил его в сообщение. Поменяется хранилище — DTO не дрогнет.

pydantic здесь ради валидации на входе (delay/soak не могут быть отрицательными)
и понятных ошибок. Внутри сервисов уже ходят доменные модели.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .enums import DeployPhase, TriggerKind, TurnSource


class PreflightReport(BaseModel):
    """Итог проверок перед выкаткой. Пустой problems — можно ехать."""

    problems: list[str] = Field(default_factory=list)
    checks: dict[str, bool] = Field(default_factory=dict)  # имя проверки → прошла ли

    @property
    def ok(self) -> bool:
        return not self.problems


class DeployCommand(BaseModel):
    """Просьба выкатить текущее рабочее дерево."""

    note: str = Field(min_length=3, description="что сделано — уйдёт в коммит")
    delay_s: int = Field(default=8, ge=0, le=600, description="пауза перед рестартом: успеть ответить в чат")
    soak_s: int = Field(default=120, ge=0, le=3600, description="сколько наблюдать после старта")
    health_timeout_s: int = Field(default=90, ge=10, le=900)
    chat_id: int | None = None
    trigger: TriggerKind = TriggerKind.ASSISTANT
    require_manual_stable: bool = Field(
        default=False,
        description="не двигать метку stable автоматически — ждать команды /stable",
    )


class DeployResult(BaseModel):
    """Что вышло из попытки выкатки (на момент запуска сторожа, не итог)."""

    ok: bool
    message: str
    prev_sha: str | None = None
    new_sha: str | None = None
    files: list[str] = Field(default_factory=list)
    preflight: PreflightReport | None = None
    deployment_id: int | None = None


class RollbackCommand(BaseModel):
    """Просьба вернуться назад. target понимает stable, sha и «N шагов назад»."""

    target: str = "stable"
    reason: str = "ручной откат"
    chat_id: int | None = None
    delay_s: int = Field(default=3, ge=0, le=600)
    trigger: TriggerKind = TriggerKind.USER


class VersionView(BaseModel):
    """Строчка для /versions: коммит плюс то, чем он кончился по данным БД."""

    sha: str
    short: str
    subject: str
    when: str
    is_current: bool = False
    is_stable: bool = False
    phase: DeployPhase | None = None
    reason: str | None = None


class TurnRequest(BaseModel):
    """Ход разговора, поставленный в очередь."""

    chat_id: int
    user_id: int | None = None
    text: str
    reply_to: int | None = None
    note: str | None = None  # что показать перед ходом (например, расшифровку голосового)
    source: TurnSource = TurnSource.TEXT
    goal_id: int | None = None  # ход сделан ради этой цели


class TurnOutcome(BaseModel):
    """Итог хода — то, что сервис отдаёт наверх и кладёт в журнал."""

    ok: bool
    status: str
    answer: str = ""
    tools: list[str] = Field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    error: str | None = None


class JournalEntry(BaseModel):
    """Найденный кусок истории — для /recall."""

    id: int
    when: datetime
    prompt: str
    answer_head: str
    status: str
    rank: float = 0.0
    passage: str = ""
    # Тот самый кусок, который совпал с запросом. Главное отличие от «нашёлся
    # такой разговор»: видно место, а не только запись. Пусто, если запись
    # найдена целиком (пустой запрос — просто последние ходы).
    said_by: str = ""     # user | assistant — кто произнёс совпавший кусок


class HealthView(BaseModel):
    """Сводка о живости — для /status и для сторожа через файл."""

    alive: bool
    pid: int | None = None
    commit: str | None = None
    uptime_s: float = 0.0
    telegram_ok: bool = False
    claude_alive: bool = False
    db_ok: bool = False
    last_beat_age_s: float | None = None


class LLMReply(BaseModel):
    """Ответ языковой модели вместе с ценой вопроса.

    Стоимость приходит от провайдера, а не считается нами по прайс-листу: тарифы
    меняются, а врать про деньги в логах — худший вид неточности.
    """

    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
