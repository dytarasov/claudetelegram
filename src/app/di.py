"""Сборка приложения: единственное место, где интерфейс встречается с реализацией.

Dishka-контейнер — это ответ на вопрос «кто кому кого подставляет». Нигде больше
в коде нет ни одного `PgTurnRepository(...)` или `ClaudeProcess(...)`: сервисы
просят порт, контейнер даёт конкретику. Поэтому:

  • в тестах достаточно собрать контейнер с другим провайдером — и сервис,
    ничего не подозревая, работает с заглушкой вместо Postgres;
  • чтобы включить семантический поиск, надо заменить NullEmbedder на настоящий
    в одном месте, а не искать по всему проекту, где создаются объекты;
  • порядок инициализации и закрытия ресурсов описан один раз, здесь.

Скоупы. Почти всё живёт в Scope.APP — процесс claude, пул соединений, сервисы
существуют всё время жизни бота, а не на один апдейт. Scope.REQUEST открывает
aiogram-интеграция на каждый апдейт; в нём ничего своего пока нет, но именно он
позволит потом добавить, например, транзакцию на апдейт, не переписывая сервисы.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dishka import AsyncContainer, Provider, Scope, make_async_container, provide

from .domain.ports import (AssistantProcess, ChunkRepository, DeploymentRepository,
                           EventRepository,
                           FactRepository, KeyValueRepository,
                           NoteRepository, Notifier, ServiceControl, SpeechToText,
                           Embedder, LLMClient, LogReader, StorageHealth,
                           GoalRepository, ReminderRepository, TermRepository,
                           TurnPresenterFactory,
                           TurnRepository, VersionControl)
from .infrastructure.claude.mcp import write_mcp_config
from .infrastructure.claude.process import ClaudeProcess
from .infrastructure.db.database import Database
from .infrastructure.db.unit_of_work import UnitOfWork
from .infrastructure.llm.null import NullEmbedder, NullLLM
from .infrastructure.llm.openai_embedder import OpenAICompatibleEmbedder
from .infrastructure.llm.openrouter import OpenRouterClient
from .infrastructure.logs.reader import FileLogReader
from .infrastructure.repositories.deployments import PgDeploymentRepository
from .infrastructure.repositories.kv import KeyValueStore
from .infrastructure.repositories.goals import PgGoalRepository
from .infrastructure.repositories.chunks import PgChunkRepository
from .infrastructure.repositories.events import PgEventRepository
from .infrastructure.repositories.facts import PgFactRepository
from .infrastructure.repositories.notes import PgNoteRepository
from .infrastructure.repositories.reminders import PgReminderRepository
from .infrastructure.repositories.stt_terms import PgTermRepository
from .infrastructure.repositories.turns import PgTurnRepository
from .infrastructure.stt.groq_engine import GroqTranscriber
from .infrastructure.stt.local_engine import LocalWhisperTranscriber
from .infrastructure.stt.service import SpeechService
from .infrastructure.system.git import GitClient
from .infrastructure.system.systemd import SystemdClient
from .infrastructure.telegram.sender import MessageSender
from .infrastructure.telegram.views import TurnViewFactory
from .services.conversation import ConversationService
from .services.health import HealthService
from .services.goals import GoalService
from .services.journal import JournalService
from .services.memory import MemoryService
from .services.pending import PendingInput
from .services.logs import LogService
from .services.proactive import ProactiveService
from .services.selfupdate import SelfUpdateService
from .services.terms import TermsService
from .services.when import WhenParser
from .settings import Settings


class CoreProvider(Provider):
    """Настройки, внешние системы, Telegram-клиент."""

    scope = Scope.APP

    @provide
    def settings(self) -> Settings:
        return Settings()

    @provide
    async def database(self, settings: Settings) -> AsyncIterator[Database]:
        """Пул на весь процесс. Миграции накатываются здесь же, при старте.

        Если база недоступна, бот всё равно должен подняться: разговор важнее
        журнала. Поэтому ошибка подключения только логируется — репозитории
        переживут её сами (см. KeyValueStore с файловым дублем).
        """
        db = Database(settings.database_dsn, settings.db_pool_min, settings.db_pool_max)
        try:
            await db.connect()
            await db.migrate()
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("postgres недоступен — работаю без журнала")
        yield db
        await db.close()

    @provide
    def git(self, settings: Settings) -> VersionControl:
        return GitClient(settings.root)

    @provide
    def systemd(self) -> ServiceControl:
        return SystemdClient()

    @provide
    def log_reader(self, settings: Settings) -> LogReader:
        return FileLogReader(settings)

    @provide
    async def bot(self, settings: Settings) -> AsyncIterator[Bot]:
        bot = Bot(token=settings.telegram_bot_token,
                  default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        yield bot
        await bot.session.close()

    @provide
    def sender(self, bot: Bot) -> MessageSender:
        return MessageSender(bot)

    # Тот же объект, но под именем порта: сервисы просят Notifier и не знают,
    # что за ним aiogram. Замена на заглушку в тестах — одна строчка здесь.
    @provide
    def notifier(self, sender: MessageSender) -> Notifier:
        return sender

    @provide
    def storage_health(self, db: Database) -> StorageHealth:
        return db


class RepositoryProvider(Provider):
    """Хранилище. Наружу отдаются порты, а не классы с SQL внутри."""

    scope = Scope.APP

    @provide
    def deployments(self, db: Database) -> DeploymentRepository:
        return PgDeploymentRepository(db)

    @provide
    def turns(self, db: Database) -> TurnRepository:
        return PgTurnRepository(db)

    @provide
    def notes(self, db: Database) -> NoteRepository:
        return PgNoteRepository(db)

    @provide
    def chunks(self, db: Database) -> ChunkRepository:
        return PgChunkRepository(db)

    @provide
    def facts(self, db: Database) -> FactRepository:
        return PgFactRepository(db)

    @provide
    def events(self, db: Database) -> EventRepository:
        return PgEventRepository(db)

    @provide
    def goals_repo(self, db: Database) -> GoalRepository:
        return PgGoalRepository(db)

    @provide
    def reminders(self, db: Database) -> ReminderRepository:
        return PgReminderRepository(db)

    @provide
    def terms(self, db: Database) -> TermRepository:
        return PgTermRepository(db)

    @provide
    def kv(self, db: Database, settings: Settings) -> KeyValueStore:
        return KeyValueStore(db, settings.state_file)

    @provide
    def kv_port(self, store: KeyValueStore) -> KeyValueRepository:
        return store

    @provide
    def unit_of_work(self, db: Database, settings: Settings) -> UnitOfWork:
        """Транзакция на несколько репозиториев — когда «всё или ничего»."""
        return UnitOfWork(db, settings.state_file)


class EngineProvider(Provider):
    """Тяжёлые внешние движки: процесс claude и распознавание речи."""

    scope = Scope.APP

    @provide
    async def process(self, settings: Settings, kv: KeyValueStore) -> AssistantProcess:
        """Процесс поднимается на том же id сессии, что был до перезапуска.

        Это и есть причина, по которой обновление кода не стирает разговор:
        id лежит в kv (БД + файл), а claude умеет --resume.
        """
        from pathlib import Path

        session_id = await kv.get("session_id")
        cwd = await kv.get("cwd")
        model = await kv.get("model")
        return ClaudeProcess(
            claude_bin=settings.claude_bin,
            cwd=Path(cwd) if cwd else settings.workspace,
            model=model or settings.claude_model,
            permission_mode=settings.permission_mode,
            session_id=session_id,
            # Инструменты памяти подключаются здесь: без конфига сессия
            # поднимется так же, просто вспоминать будет нечем.
            mcp_config=write_mcp_config(settings.run_dir, settings.venv_python,
                                        settings.src_dir),
        )

    @provide
    def groq(self, settings: Settings) -> GroqTranscriber:
        return GroqTranscriber(settings)

    @provide
    def local_whisper(self, settings: Settings) -> LocalWhisperTranscriber:
        return LocalWhisperTranscriber(settings)

    @provide
    def llm(self, settings: Settings) -> LLMClient:
        """Чат-модель общего назначения. Без ключа — честная заглушка.

        Заглушка, а не исключение при сборке: отсутствие внешней модели —
        обычное состояние системы, разговор ведёт claude в отдельном процессе,
        и всё остальное обязано работать.
        """
        return OpenRouterClient(settings) if settings.llm_api_key else NullLLM()

    @provide
    def embedder(self, settings: Settings) -> Embedder:
        """Эмбеддер. Провайдер задаётся адресом, поэтому его можно увести
        куда угодно, не трогая чат-модель."""
        if settings.embeddings_api_key and settings.embeddings_model:
            return OpenAICompatibleEmbedder(settings)
        return NullEmbedder()

    @provide
    def stt(self, settings: Settings, groq: GroqTranscriber,
            local: LocalWhisperTranscriber) -> SpeechToText:
        return SpeechService(settings, groq, local)


class ServiceProvider(Provider):
    """Бизнес-логика. Зависит только от портов и настроек."""

    scope = Scope.APP

    @provide
    def presenters(self, sender: MessageSender, settings: Settings,
                   process: AssistantProcess) -> TurnPresenterFactory:
        """Презентер знает про расход контекста, поэтому получает его функцией
        от процесса — иначе вьюхе пришлось бы тащить весь процесс целиком."""
        return TurnViewFactory(sender, settings, process.context_usage)

    @provide
    def terms_service(self, repository: TermRepository) -> TermsService:
        return TermsService(repository)

    @provide
    def conversation(self, process: AssistantProcess, notifier: Notifier,
                     presenters: TurnPresenterFactory, turns: TurnRepository,
                     kv: KeyValueRepository, terms: TermsService,
                     settings: Settings) -> ConversationService:
        return ConversationService(process, notifier, presenters, turns, kv, terms, settings)

    @provide
    def selfupdate(self, settings: Settings, git: VersionControl, systemd: ServiceControl,
                   deployments: DeploymentRepository) -> SelfUpdateService:
        return SelfUpdateService(settings, git, systemd, deployments)

    @provide
    def health(self, settings: Settings, process: AssistantProcess, notifier: Notifier,
               storage: StorageHealth, git: VersionControl) -> HealthService:
        return HealthService(settings, process, notifier, storage, git)

    @provide
    def journal(self, turns: TurnRepository, notes: NoteRepository,
                chunks: ChunkRepository, embedder: Embedder) -> JournalService:
        return JournalService(turns, notes, chunks, embedder)

    @provide
    def memory(self, facts: FactRepository, events: EventRepository,
               reminders: ReminderRepository, journal: JournalService,
               settings: Settings) -> MemoryService:
        """Память как один вход во все слои — ею пользуется MCP-сервер."""
        return MemoryService(facts, events, reminders, journal, settings)

    @provide
    def pending(self) -> PendingInput:
        """Ожидания ввода живут в памяти процесса — см. services/pending.py."""
        return PendingInput()

    @provide
    def logs(self, reader: LogReader) -> LogService:
        return LogService(reader)

    @provide
    def when_parser(self, llm: LLMClient, settings: Settings) -> WhenParser:
        return WhenParser(llm, settings)

    @provide
    def goals(self, repository: GoalRepository, notifier: Notifier,
              settings: Settings) -> GoalService:
        return GoalService(repository, notifier, settings)

    @provide
    def proactive(self, reminders: ReminderRepository, notifier: Notifier,
                  settings: Settings) -> ProactiveService:
        return ProactiveService(reminders, notifier, settings)


def build_container() -> AsyncContainer:
    """Собрать контейнер приложения. Точка входа зовёт это ровно один раз."""
    return make_async_container(
        CoreProvider(), RepositoryProvider(), EngineProvider(), ServiceProvider(),
    )
