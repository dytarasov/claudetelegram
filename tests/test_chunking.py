"""Нарезка текста на куски.

Проверяется то, что портит поиск, если сделать наивно: разрез посреди фразы,
бесполезные огрызки в пару строк, потеря мысли на границе кусков и зацикливание
на тексте без единого шва.
"""
from __future__ import annotations

from app.services.chunking import MAX, chunk_turn, split


def test_short_text_stays_one_piece():
    assert split("привет, как дела") == ["привет, как дела"]
    assert split("") == []
    assert split("   \n  ") == []


def test_long_text_is_cut_by_paragraphs():
    text = "\n\n".join(f"Абзац номер {i}. " + "слово " * 40 for i in range(8))
    pieces = split(text)
    assert len(pieces) > 1
    assert all(len(p) <= MAX for p in pieces)
    # Ни один кусок не начинается с обрывка слова.
    assert all(p[0].isupper() or p[0].isalnum() for p in pieces)


def test_no_infinite_loop_on_seamless_text():
    """Сплошная простыня без знаков препинания режется жёстко, но режется."""
    pieces = split("а" * 5000)
    assert len(pieces) >= 3
    assert all(len(p) <= MAX for p in pieces)


def test_pieces_overlap_so_a_thought_on_the_border_is_not_lost():
    text = ("Первая часть. " * 150) + "ГРАНИЦА " + ("Вторая часть. " * 150)
    pieces = split(text)
    assert len(pieces) > 1, "текст обязан был разрезаться"
    joined = sum(len(p) for p in pieces)
    assert joined > len(text), "куски обязаны перекрываться"


def test_tiny_tail_is_glued_to_previous_piece():
    """Огрызок в полстроки бесполезен: у него нет контекста, вектор случайный."""
    text = "предложение раз. " * 120 + "хвост."
    pieces = split(text)
    assert all(len(p) > 100 for p in pieces)


def test_question_and_answer_are_chunked_separately():
    """Склеенные, они дают вектор, не похожий ни на вопрос, ни на ответ."""
    pieces = chunk_turn("почему пусто?", "потому что подсказка ломает модель")
    assert [role for role, _ in pieces] == ["user", "assistant"]
    assert pieces[0][1] == "почему пусто?"


def test_empty_answer_gives_only_the_question():
    assert chunk_turn("вопрос", "") == [("user", "вопрос")]
