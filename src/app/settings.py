"""Настройки приложения — единственное место, где мы читаем окружение.

Раньше config.py раздавал модульные константы, и любой файл мог дотянуться до
os.environ. Теперь настройки — обычный объект, который выдаёт DI-контейнер:
в тестах достаточно собрать Settings(...) руками, без .env и переменных среды.

Значения берутся из .env в корне проекта (файл в .gitignore, в репозиторий не
попадает; образец — .env.example).
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Всё, что настраивается снаружи. Имена полей = имена переменных в .env."""

    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Telegram ----
    telegram_bot_token: str = ""
    allowed_user_ids: set[int] = Field(default_factory=set)

    # ---- claude ----
    claude_bin: str = "claude"
    claude_model: str = "opus"
    permission_mode: str = "bypassPermissions"
    workspace: Path = ROOT / "workspace"

    # ---- распознавание речи ----
    groq_api_key: str = ""
    groq_stt_model: str = "whisper-large-v3-turbo"
    stt_language: str = "ru"
    stt_prompt: str = (
        "Claude Code, Telegram, Python, systemd, nginx, docker, git, bash, API, "
        "сервер, деплой, репозиторий, контекст, компакт, токены, dishka, postgres"
    )
    # Русский файнтюн large-v3-turbo: на замере по Golos он заметно точнее
    # базовых моделей, а turbo-декодер (4 слоя вместо 32) делает его пригодным
    # для CPU. Цена — ~1.7 ГБ резидентной памяти и скорость около реального
    # времени на двух ядрах: минутное голосовое расшифровывается примерно минуту.
    local_stt_model: str = "dvislobokov/faster-whisper-large-v3-turbo-russian"
    # Подсказка для ЛОКАЛЬНОГО движка — намеренно отдельная от stt_prompt и по
    # умолчанию пустая. Грабли, на которые уже наступили: русский файнтюн
    # large-v3-turbo от initial_prompt со списком терминов выдаёт пустую строку
    # вместо текста — молча, без ошибки. Базовые модели такой прошивки не имеют,
    # поэтому подсказка осталась настраиваемой, но включать её нужно осознанно
    # и проверив на своей модели. Для Groq подсказка живёт в stt_prompt и работает.
    local_stt_prompt: str = ""
    local_stt_fallback: bool = True

    # ---- внешние модели ----
    # Чат-модели: OpenRouter (или любой другой OpenAI-совместимый шлюз).
    # Это НЕ тот claude, что ведёт разговор, — тот живёт отдельным процессом.
    # Здесь короткие служебные вызовы: сжать, разметить, подсказать.
    llm_api_key: str = ""
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = "anthropic/claude-opus-5"

    # Эмбеддинги. Ключ и адрес отдельные, хотя сейчас за ними тот же OpenRouter:
    # у него есть /embeddings, просто эти модели не попадают в каталог /models —
    # именно поэтому я сперва решил, что их там нет. Разделение оставлено, чтобы
    # эмбеддер можно было увести к другому провайдеру, не трогая чат-модель.
    embeddings_api_key: str = ""
    embeddings_base_url: str = "https://openrouter.ai/api/v1"
    embeddings_model: str = "baai/bge-m3"
    # Обязан совпадать с типом колонки vector(N) в БД. Не совпало — поможет
    # `cli.py embeddings resize`, он и колонку поменяет, и векторы обнулит.
    embeddings_dim: int = 1024  # столько отдаёт bge-m3
    # Часть провайдеров умеет обрезать вектор на своей стороне (параметр
    # dimensions). Поддерживают не все, поэтому по умолчанию не просим.
    embeddings_send_dimensions: bool = False
    embeddings_max_chars: int = 8000
    # Как часто дозагружать векторы для новых записей.
    embeddings_index_interval_s: int = 300

    # ---- хранилище ----
    database_dsn: str = ""
    db_pool_min: int = 1
    db_pool_max: int = 4

    # ---- поведение ----
    log_level: str = "INFO"
    edit_interval: float = 2.0
    # Режим стрима частичного ответа, пока модель печатает:
    #   "edit"  — правка одного сообщения раз в edit_interval. Без фликера
    #             везде, но рвано (обновление раз в пару секунд).
    #   "draft" — sendMessageDraft: черновик в строке ввода. Плавно на
    #             мобиле, но десктоп/мак перерисовывает поле целиком на
    #             каждом апдейте → текст мигает.
    #   "rich"  — sendRichMessageDraft (Bot API 9.x): партиал летит как
    #             rich-сообщение и рисуется пузырём в ленте, а не в поле
    #             ввода — цель убрать мигание на десктопе, сохранив плавность.
    # Драфтовые режимы эфемерны (30с), финал всё равно уходит обычным
    # сообщением через finish(). При отказе метода режим сам падает в "edit".
    stream_mode: str = "edit"
    draft_interval: float = 0.7
    file_fallback_chars: int = 20000
    heartbeat_interval_s: float = 5.0

    # Корень проекта. Именно поле, а не константа: от него считаются все пути,
    # и тесту нужно уметь увести их во временный каталог. Пока это была
    # модульная ROOT, тесты писали в боевой run/update.json и затирали метку
    # текущей выкатки — а гоняются они в preflight перед каждым деплоем.
    root_dir: Path = ROOT

    # ---- проактивность ----
    # Часовой пояс человека, а не сервера. Сервер живёт в CEST, человек — в
    # Москве, и «напомни в 11» означает одиннадцать по его часам. Хранится всё
    # в UTC (timestamptz), пояс нужен только для разбора фраз и показа.
    user_timezone: str = "Europe/Moscow"
    # Ночью бот молчит: всё, чему пришёл срок, ждёт конца тихих часов.
    quiet_hours_from: int = 23
    quiet_hours_to: int = 8
    proactive_tick_s: int = 30
    # Цели: потолки по умолчанию. Автономность без них — счётчик, который
    # крутится, пока человек спит.
    goal_tick_s: int = 60
    goal_cadence_min: int = 60
    goal_budget_usd: float = 3.0
    goal_max_turns_day: int = 8

    # ---- самодопил ----
    service_unit: str = "claude-tg"
    stable_tag: str = "stable"
    deploy_soak_s: int = 120
    deploy_health_timeout_s: int = 90

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def _parse_ids(cls, raw: object) -> object:
        """ALLOWED_USER_IDS в .env — строка «123, 456», а не JSON-список.

        pydantic-settings до валидатора пытается разобрать значение как JSON, и
        одиночный «494317179» доезжает сюда уже целым числом — поэтому разбираем
        все три случая: число, строку и готовую коллекцию.
        """
        if isinstance(raw, int):
            return {raw}
        if isinstance(raw, str):
            return {int(x) for x in raw.replace(",", " ").split() if x.strip().isdigit()}
        if isinstance(raw, (list, tuple, set)):
            return {int(x) for x in raw}
        return raw

    @field_validator("workspace", mode="before")
    @classmethod
    def _expand(cls, raw: object) -> object:
        return Path(str(raw)).expanduser() if raw else raw

    # ---- производные пути: всё остальное считается от корня проекта ----
    @property
    def tz(self):
        """Пояс человека. Отдельным свойством, чтобы не тащить zoneinfo всюду."""
        from zoneinfo import ZoneInfo
        return ZoneInfo(self.user_timezone)

    @property
    def root(self) -> Path:
        return self.root_dir

    @property
    def src_dir(self) -> Path:
        return self.root_dir / "src"

    @property
    def run_dir(self) -> Path:
        """Рантайм-мусор: пульс, лог сторожа, метка выкатки. В .gitignore."""
        return self.root_dir / "run"

    @property
    def upload_dir(self) -> Path:
        return self.workspace / "uploads"

    @property
    def state_file(self) -> Path:
        """Резервная копия состояния на случай, когда БД недоступна."""
        return self.root_dir / "state.json"

    @property
    def heartbeat_file(self) -> Path:
        return self.run_dir / "heartbeat.json"

    @property
    def update_file(self) -> Path:
        return self.run_dir / "update.json"

    @property
    def guard_script(self) -> Path:
        return self.root_dir / "scripts" / "guard.sh"

    @property
    def venv_python(self) -> Path:
        return self.root_dir / "venv" / "bin" / "python"

    def problems(self) -> list[str]:
        """Что мешает запуску. Пустой список — можно стартовать."""
        out = []
        if not self.telegram_bot_token:
            out.append("TELEGRAM_BOT_TOKEN не задан в .env")
        if not self.allowed_user_ids:
            out.append(
                "ALLOWED_USER_IDS пуст — бот с полными правами на сервере обязан "
                "иметь белый список Telegram ID"
            )
        if not self.database_dsn:
            out.append("DATABASE_DSN не задан — журнал и история выкаток работать не будут")
        return out
