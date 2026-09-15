"""Интерфейс: палитра кнопок, вёрстка блоков и отсутствие картинок.

Последнее — не вкусовщина, а требование: эмодзи в этом боте заменены стилями
кнопок и разметкой. Тест держит это, потому что при быстрой правке «звёздочку
для красоты» вернуть проще всего.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.infrastructure.telegram import ui

SRC = Path(__file__).resolve().parents[1] / "src" / "app"

# Диапазоны эмодзи. Стрелка «→» и типографские знаки сюда не попадают —
# они часть текста, а не картинки.
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⬀-⯿️]")


def test_palette_is_limited_to_what_telegram_accepts():
    """Других значений API не принимает — проверено запросом к нему."""
    assert {ui.DEFAULT, ui.PRIMARY, ui.SUCCESS, ui.DANGER} == {
        "default", "primary", "success", "danger"}


def test_button_carries_style_and_disabled_state():
    btn = ui.button("Прервать", "ui:stop", ui.DANGER, disabled=True, why="ход не идёт")
    assert btn.style == "danger"
    # Выключенная кнопка не исчезает, а объясняет причину при нажатии.
    assert btn.disabled.text == "ход не идёт"
    assert ui.button("Обновить", "ui:status").disabled is None


def test_copy_button_needs_no_handler():
    """Копирование хеша делает клиент Telegram, обработчик не нужен."""
    btn = ui.copy_button("Копировать", "abc1234")
    assert btn.copy_text.text == "abc1234"
    assert btn.callback_data is None


def test_confirm_offers_action_and_escape():
    markup = ui.confirm("Откатить", "ui:rollback:abc")
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == ["Откатить", "Отмена"]
    assert markup.inline_keyboard[0][0].style == "danger"


def test_keyboard_skips_empty_rows():
    markup = ui.keyboard([], [ui.button("Одна", "x")], [])
    assert len(markup.inline_keyboard) == 1


def test_long_output_is_collapsible():
    """Длинный лог не должен занимать весь экран целиком."""
    assert ui.mono_block("строка").startswith("<blockquote expandable>")
    assert ui.quote("текст", expandable=False).startswith("<blockquote>")


def test_field_escapes_values():
    assert "<script>" not in ui.field("путь", "<script>", mono=True)


def test_state_is_a_word_not_a_circle():
    assert ui.state(True) == "норма" and ui.state(False) == "СБОЙ"


@pytest.mark.parametrize("path", sorted(
    p for p in SRC.rglob("*.py")
    if p.parent.name in ("routers", "middlewares", "telegram", "services", "presentation")
))
def test_no_emoji_in_user_facing_code(path):
    found = EMOJI.findall(path.read_text(encoding="utf-8"))
    assert not found, f"{path.name}: вернулись картинки {found[:5]}"
