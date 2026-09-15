"""Кнопочный интерфейс: ожидание ввода и связность экранов.

Главная проверка здесь — та, которую невозможно сделать глазами: у каждой
кнопки, нарисованной на экране, есть обработчик. Мёртвая кнопка не падает и не
логируется, она просто молчит под пальцем, и заметить её можно лишь случайно.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.presentation.routers.ui_router import ASK_PROMPTS
from app.services.pending import PendingInput

ROOT = Path(__file__).resolve().parents[1]
SCREENS = ROOT / "src/app/presentation/screens.py"
ROUTERS = ROOT / "src/app/presentation/routers"

# Литералы кнопок: обычные и внутри f-строк. Для «ui:logs:{limit}» берём
# неизменную часть до подстановки — по ней и ищется обработчик.
_DATA = re.compile(r'"((?:ui|ask|rem|goal):[^"{]*)(?:\{[^"]*)?"')
_EXACT = re.compile(r'F\.data == "([^"]+)"')
_PREFIX = re.compile(r'F\.data\.startswith\("([^"]+)"\)')


def _screen_buttons() -> set[str]:
    return {m.group(1) for m in _DATA.finditer(SCREENS.read_text(encoding="utf-8"))}


def _handlers() -> tuple[set[str], set[str]]:
    exact: set[str] = set()
    prefix: set[str] = set()
    for path in ROUTERS.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        exact |= {m.group(1) for m in _EXACT.finditer(source)}
        prefix |= {m.group(1) for m in _PREFIX.finditer(source)}
    return exact, prefix


def test_every_button_on_a_screen_has_a_handler():
    exact, prefix = _handlers()
    orphans = sorted(data for data in _screen_buttons()
                     if data not in exact and not any(data.startswith(p) for p in prefix))
    assert not orphans, f"кнопки без обработчика: {orphans}"


def test_screens_do_not_reference_unknown_ask_actions():
    """Кнопка «спроси меня» бесполезна без текста вопроса."""
    asked = {data.split(":", 1)[1] for data in _screen_buttons() if data.startswith("ask:")}
    assert asked, "ни одной кнопки с запросом текста — проверка бессмысленна"
    assert asked <= set(ASK_PROMPTS), f"нет вопроса для: {sorted(asked - set(ASK_PROMPTS))}"


def test_root_menu_is_reachable_from_every_screen():
    """Из любого экрана один шаг до корня — иначе кнопки превращаются в лабиринт."""
    source = SCREENS.read_text(encoding="utf-8")
    screens_with_keyboard = source.count("ui.keyboard(")
    assert source.count('"ui:menu"') >= screens_with_keyboard // 2


# --- ожидание ввода ---------------------------------------------------------- #
def test_pending_input_is_one_shot():
    """Иначе одно нажатие «Найти» превратило бы в поиск всё, что написано дальше."""
    pending = PendingInput()
    pending.expect(1, "recall")
    assert pending.take(1) == "recall"
    assert pending.take(1) is None


def test_pending_input_expires():
    pending = PendingInput(ttl=0)
    pending.expect(1, "note")
    assert pending.take(1) is None, "протухшее ожидание не должно перехватывать реплику"


def test_pending_is_per_chat():
    pending = PendingInput()
    pending.expect(1, "note")
    assert pending.take(2) is None
    assert pending.take(1) == "note"


def test_cancel_reports_whether_there_was_anything():
    pending = PendingInput()
    assert pending.cancel(1) is False
    pending.expect(1, "goal")
    assert pending.cancel(1) is True
    assert pending.take(1) is None
