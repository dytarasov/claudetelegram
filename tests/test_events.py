"""Формат ленты событий.

Лог читают в аварии, поэтому у него есть контракт, а не «как получилось»:
компонент, точечное имя события и поля key=value. Тесты держат этот контракт —
иначе он расползётся при первой же спешной правке.
"""
from __future__ import annotations

import logging

import pytest

from app.services import events


@pytest.fixture
def captured(caplog):
    caplog.set_level(logging.INFO, logger="event")
    return caplog


def _line(captured) -> str:
    record = captured.records[-1]
    return record.getMessage()


def test_line_starts_with_component_and_event(captured):
    events.turn_interrupted()
    line = _line(captured)
    assert line.split()[0] == "ConversationService"
    assert line.split()[1] == "turn.interrupted"


def test_fields_are_key_value_pairs(captured):
    events.turn_finished("ok", 84.0, 6, 12480, 0.0432)
    line = _line(captured)
    assert "status=ok" in line
    assert "duration=84.0s" in line
    assert "tools=6" in line
    assert "tokens=12480" in line
    assert "cost_usd=0.043" in line


def test_values_with_spaces_are_quoted(captured):
    events.turn_started("voice", "сделай логи посерьёзнее")
    assert 'text="сделай логи посерьёзнее"' in _line(captured)


def test_newlines_never_break_a_line(captured):
    events.turn_failed("failed", "первая строка\nвторая строка")
    line = _line(captured)
    assert "\n" not in line
    assert "первая строка вторая строка" in line


def test_empty_and_none_fields_are_dropped(captured):
    events.deploy_verdict("ok", "заметка", None)
    line = _line(captured)
    assert "reason=" not in line
    assert "phase=ok" in line


def test_booleans_render_as_true_false(captured):
    events.health_changed("postgres", False)
    assert "reachable=false" in _line(captured)


@pytest.mark.parametrize("call,level", [
    (lambda: events.turn_finished("ok", 1.0, 0, 0, 0.0), "INFO"),
    (lambda: events.turn_failed("failed", "boom"), "WARNING"),
    (lambda: events.access_denied(1, "x"), "WARNING"),
    (lambda: events.claude_died("boom"), "WARNING"),
    (lambda: events.deploy_verdict("rolled_back", "n", "r"), "WARNING"),
    (lambda: events.deploy_verdict("ok", "n", None), "INFO"),
])
def test_alarming_events_are_warnings(captured, call, level):
    call()
    assert captured.records[-1].levelname == level


def test_no_emoji_anywhere_in_the_vocabulary():
    """Лог — не место для картинок: они мешают отличать важное от фонового."""
    import inspect

    source = inspect.getsource(events)
    assert not any(ord(ch) > 0x2100 for ch in source), "в ленте событий появились эмодзи"
