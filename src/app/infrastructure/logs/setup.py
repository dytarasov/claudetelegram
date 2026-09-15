"""Настройка логов: что пишем, куда и насколько подробно.

Три потока, у каждого своя задача:

  • journald (stdout)   — как было; смотреть через journalctl -u claude-tg;
  • run/app.log         — подробный технический лог с ротацией. Нужен потому,
                          что журнал systemd неудобно листать из телеграма и
                          нельзя отдать файлом;
  • run/events.log      — короткая человеческая лента: что произошло, а не как
                          оно устроено внутри. Именно её показывает /logs.

Разделение важно на практике. Подробный лог отвечает на вопрос «почему сломалось»
и полон технических деталей. Лента событий отвечает на «что вообще происходит» —
одна строка на осмысленное событие, без имён модулей и трассировок. Смешивать их
нельзя: подробности топят смысл, а без подробностей нечего чинить.

Отдельная забота — шум библиотек. httpx рассказывает про каждый HTTP-запрос к
Hugging Face, aiogram — про каждый апдейт, faster-whisper — про каждый кусок
звука. В подробном логе это мусор, в ленте — тем более, поэтому им подняты
уровни. Правило: если строку нельзя использовать при разборе аварии, ей не место
в логе на уровне INFO.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from ...settings import Settings

# Имя логгера для человеческой ленты. Пишут в него через services/events.py,
# напрямую лучше не дёргать — там собраны все формулировки разом.
EVENT_LOGGER = "event"

# Болтливые чужие логгеры: значение — уровень, ниже которого молчим.
NOISY = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "huggingface_hub": logging.ERROR,
    "huggingface_hub.utils._http": logging.ERROR,
    "faster_whisper": logging.WARNING,
    "aiogram.event": logging.WARNING,
    "asyncio": logging.WARNING,
    "aiosqlite": logging.WARNING,
}

DETAILED_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
# Уровень четырьмя буквами (INFO/WARN/ERRO): столбец ровный, а важное видно
# боковым зрением при прокрутке. Сами события форматирует services/events.py —
# там же задана ширина колонок компонента и имени события.
EVENT_FORMAT = "%(asctime)s %(levelname).4s %(message)s"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

MAX_BYTES = 8 * 1024 * 1024
BACKUPS = 3


def setup_logging(settings: Settings) -> None:
    """Собрать все три потока. Зовётся один раз при старте, до всего остального."""
    settings.run_dir.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, settings.log_level, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):  # перезапуск в тестах не должен множить вывод
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(DETAILED_FORMAT, TIME_FORMAT))
    root.addHandler(console)

    detailed = RotatingFileHandler(
        settings.run_dir / "app.log", maxBytes=MAX_BYTES, backupCount=BACKUPS,
        encoding="utf-8",
    )
    detailed.setFormatter(logging.Formatter(DETAILED_FORMAT, TIME_FORMAT))
    root.addHandler(detailed)

    events = RotatingFileHandler(
        settings.run_dir / "events.log", maxBytes=MAX_BYTES, backupCount=BACKUPS,
        encoding="utf-8",
    )
    events.setFormatter(logging.Formatter(EVENT_FORMAT, TIME_FORMAT))
    event_logger = logging.getLogger(EVENT_LOGGER)
    event_logger.setLevel(logging.INFO)
    for handler in list(event_logger.handlers):
        event_logger.removeHandler(handler)
    event_logger.addHandler(events)
    # propagate оставляем: события видны и в подробном логе, и в journald —
    # чтобы при разборе аварии не приходилось сводить два файла вручную.

    for name, noisy_level in NOISY.items():
        logging.getLogger(name).setLevel(noisy_level)
