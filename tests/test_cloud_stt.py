"""Облачный STT: разбор ответа провайдера и выбор ключа — без сети.

Сеть не трогаем: проверяем чистую логику вытаскивания текста из ответа (JSON или
голый текст) и то, что ключ STT по умолчанию наследуется от ключа OpenRouter.
"""
from __future__ import annotations

from app.infrastructure.stt.cloud_engine import CloudTranscriber, extract_text
from app.settings import Settings


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, telegram_bot_token="t", allowed_user_ids="1",
                    database_dsn="d", **kw)


def test_extract_text_from_json():
    assert extract_text('{"text": "привет мир", "usage": {"seconds": 1}}') == "привет мир"


def test_extract_text_from_plain_body():
    assert extract_text("просто текст") == "просто текст"


def test_extract_text_trims():
    assert extract_text('{"text": "  с пробелами  "}') == "с пробелами"


def test_stt_key_falls_back_to_openrouter_key():
    # Отдельный ключ STT не задан — берём ключ чат-моделей (тот же OpenRouter).
    assert _settings(llm_api_key="or-key").stt_key == "or-key"


def test_explicit_stt_key_wins():
    s = _settings(llm_api_key="or-key", stt_api_key="dedicated")
    assert s.stt_key == "dedicated"


def test_usable_reflects_key_presence():
    assert CloudTranscriber(_settings(llm_api_key="or-key")).usable is True
    assert CloudTranscriber(_settings()).usable is False


def test_transcriptions_url_built_from_base():
    s = _settings(llm_api_key="k", stt_base_url="https://openrouter.ai/api/v1")
    assert CloudTranscriber(s)._url == "https://openrouter.ai/api/v1/audio/transcriptions"
