"""Конструктор интерфейса: кнопки, клавиатуры, блоки текста.

Зачем отдельный модуль. Кнопки в боте расползаются по роутерам, и через месяц
одна и та же по смыслу кнопка называется в трёх местах по-разному и красится
по-разному. Здесь собраны и палитра, и правила вёрстки, поэтому интерфейс
остаётся однородным, даже когда экраны пишут в разное время.

Что даёт Telegram (проверено на живом API, а не по памяти):
  • style у кнопки: default | primary | success | danger — других нет;
  • disabled — кнопка видна, но не нажимается: удобно показывать состояние,
    не убирая элемент и не ломая раскладку;
  • copy_text — копирование в буфер по нажатию, без обработчика на нашей
    стороне: идеально для хешей коммитов и путей;
  • <blockquote expandable> — сворачиваемая цитата: длинный лог не занимает
    весь экран, но раскрывается касанием.

Эмодзи здесь нет намеренно. Цвет и вес кнопке задаёт style, структуру тексту —
разметка; картинки в этой роли только шумят.
"""
from __future__ import annotations

from aiogram.types import (CopyTextButton, DisabledButton, InlineKeyboardButton,
                           InlineKeyboardMarkup)

from .formatting import esc

# Палитра. Больше значений Telegram не принимает — проверено запросом к API.
DEFAULT = "default"
PRIMARY = "primary"     # основное действие экрана
SUCCESS = "success"     # подтверждение, «готово»
DANGER = "danger"       # необратимое: откат, сброс сессии


def button(text: str, data: str, style: str = DEFAULT,
           disabled: bool = False, why: str = "сейчас недоступно") -> InlineKeyboardButton:
    """Кнопка. Выключенная остаётся на месте и объясняет, почему не работает.

    Telegram ждёт в поле disabled не флаг, а объект с текстом всплывающего
    пояснения — это и лучше: «прервать нечего, ход не идёт» понятнее, чем
    исчезнувшая кнопка или молчаливое нажатие.
    """
    return InlineKeyboardButton(
        text=text, callback_data=data, style=style,
        disabled=DisabledButton(text=why) if disabled else None,
    )


def copy_button(text: str, payload: str) -> InlineKeyboardButton:
    """Кнопка, кладущая текст в буфер обмена. Обработчик не нужен."""
    return InlineKeyboardButton(text=text, copy_text=CopyTextButton(text=payload))


def keyboard(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


def confirm(action_text: str, action_data: str, style: str = DANGER) -> InlineKeyboardMarkup:
    """Двухшаговое подтверждение для необратимых действий.

    Правило простое: если действие нельзя отменить одной кнопкой, спрашиваем.
    Откат версии и сброс сессии стоят одного лишнего касания.
    """
    return keyboard([button(action_text, action_data, style),
                     button("Отмена", "ui:dismiss", DEFAULT)])


# --------------------------------------------------------------------------- #
#  текстовые блоки
# --------------------------------------------------------------------------- #
def title(text: str) -> str:
    return f"<b>{esc(text)}</b>"


def field(name: str, value: str, mono: bool = False) -> str:
    """Строка «поле — значение» с выровненным по смыслу оформлением."""
    shown = f"<code>{esc(value)}</code>" if mono else esc(value)
    return f"{esc(name)}: {shown}"


def quote(text: str, expandable: bool = True) -> str:
    """Цитата, по умолчанию сворачиваемая.

    Длинный вывод (логи, список версий) не должен занимать весь экран: под
    свёрнутой цитатой видно первые строки, остальное раскрывается касанием.
    """
    tag = "<blockquote expandable>" if expandable else "<blockquote>"
    return f"{tag}{text}</blockquote>"


def mono_block(text: str, expandable: bool = True) -> str:
    """Моноширинный блок в сворачиваемой цитате — для логов и вывода команд."""
    return quote(f"<pre>{esc(text)}</pre>", expandable)


def state(ok: bool) -> str:
    """Состояние словом, а не кружком: «норма» / «сбой»."""
    return "норма" if ok else "СБОЙ"
