"""Общая настройка тестов.

Две вещи. Первая — сделать пакет app импортируемым без установки.

Вторая тоньше: бот читает .env в свои переменные окружения, а claude запускается
его потомком, поэтому у тестов, запущенных из чата, в окружении уже лежат
настоящие TELEGRAM_BOT_TOKEN и прочее. Тест, который проверяет поведение «когда
ничего не настроено», из-за этого проходил бы вхолостую. Чистим окружение — так
результат одинаков и в чате, и в терминале, и внутри preflight.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

APP_ENV_VARS = [
    "TELEGRAM_BOT_TOKEN", "ALLOWED_USER_IDS", "DATABASE_DSN", "GROQ_API_KEY",
    "GROQ_STT_MODEL", "STT_LANGUAGE", "STT_PROMPT", "LOCAL_STT_MODEL",
    "LOCAL_STT_FALLBACK", "CLAUDE_BIN", "CLAUDE_MODEL", "PERMISSION_MODE",
    "WORKSPACE", "LOG_LEVEL", "EDIT_INTERVAL", "FILE_FALLBACK_CHARS",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in APP_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
