"""Локальный движок распознавания — на подставной модели, без загрузки весов.

Тест закрывает конкретные грабли: русский файнтюн large-v3-turbo от
initial_prompt возвращает пустую строку. Общий stt_prompt (он нужен Groq) не
должен доезжать до локальной модели, иначе голосовые молча перестают работать.

На облачной установке (--cloud-stt) faster-whisper не ставится вовсе, а метод
transcribe() тогда сразу отдаёт STTError. Гонять эти сценарии там незачем,
поэтому весь модуль пропускается, если пакета нет: иначе preflight на слабом
сервере валился бы на отсутствующей зависимости.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("faster_whisper")

from app.infrastructure.stt.local_engine import LocalWhisperTranscriber  # noqa: E402
from app.settings import Settings  # noqa: E402


class FakeSegment:
    def __init__(self, text): self.text = text


class FakeModel:
    """Запоминает, с чем её позвали, и отвечает по сценарию."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def transcribe(self, path, **kw):
        self.calls.append(kw)
        text = self.answers.pop(0) if self.answers else ""
        return ([FakeSegment(text)] if text else []), None


def _engine(model, **overrides):
    settings = Settings(_env_file=None, telegram_bot_token="t", allowed_user_ids="1",
                        database_dsn="d", stt_prompt="Claude Code, systemd, dishka",
                        **overrides)
    engine = LocalWhisperTranscriber(settings)
    engine._model = model
    return engine


async def test_general_prompt_never_reaches_local_model():
    model = FakeModel(["привет"])
    await _engine(model).transcribe(Path("/tmp/x.oga"), "ru")
    assert model.calls[0]["initial_prompt"] is None


async def test_local_prompt_is_used_when_set_explicitly():
    model = FakeModel(["привет"])
    engine = _engine(model, local_stt_prompt="дишка, постгрес")
    await engine.transcribe(Path("/tmp/x.oga"), "ru")
    assert model.calls[0]["initial_prompt"] == "дишка, постгрес"


async def test_empty_result_triggers_retry_without_vad():
    model = FakeModel(["", "всё-таки речь"])
    text = await _engine(model).transcribe(Path("/tmp/x.oga"), "ru")
    assert text == "всё-таки речь"
    assert [c["vad_filter"] for c in model.calls] == [True, False]


async def test_single_pass_when_first_attempt_succeeds():
    model = FakeModel(["сразу распознал"])
    text = await _engine(model).transcribe(Path("/tmp/x.oga"), "ru")
    assert text == "сразу распознал"
    assert len(model.calls) == 1


async def test_broken_model_raises_stt_error():
    from app.infrastructure.stt.base import STTError

    class Broken:
        def transcribe(self, *a, **kw): raise RuntimeError("веса битые")

    with pytest.raises(STTError):
        await _engine(Broken()).transcribe(Path("/tmp/x.oga"), "ru")
