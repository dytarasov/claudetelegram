"""Мастер установки: проверяем чистую логику записи .env, без Телеграма.

apply_env — единственное место, где мастер меняет конфиг, поэтому и покрыто:
существующие ключи должны переписываться на месте (комментарии и порядок целы),
недостающие — дописываться, а секреты пользователя не должны затираться зря.
"""
from __future__ import annotations

from app.setup import apply_env


def test_updates_existing_key_in_place():
    src = "TELEGRAM_BOT_TOKEN=old\nCLAUDE_MODEL=opus\n"
    out = apply_env(src, {"CLAUDE_MODEL": "sonnet"})
    assert "CLAUDE_MODEL=sonnet" in out
    assert "CLAUDE_MODEL=opus" not in out
    assert "TELEGRAM_BOT_TOKEN=old" in out  # чужие строки не тронуты


def test_appends_missing_key():
    out = apply_env("CLAUDE_MODEL=opus\n", {"ALLOWED_USER_IDS": "42"})
    assert "ALLOWED_USER_IDS=42" in out
    assert "CLAUDE_MODEL=opus" in out


def test_comments_and_order_preserved():
    src = "# токен\nTELEGRAM_BOT_TOKEN=t\n# модель\nCLAUDE_MODEL=opus\n"
    out = apply_env(src, {"CLAUDE_MODEL": "haiku"}).splitlines()
    assert out[0] == "# токен"
    assert out[2] == "# модель"
    assert out[3] == "CLAUDE_MODEL=haiku"


def test_only_full_key_matches_not_substring():
    # GROQ_API_KEY не должен зацепить строку GROQ_STT_MODEL и наоборот.
    src = "GROQ_STT_MODEL=whisper\nGROQ_API_KEY=\n"
    out = apply_env(src, {"GROQ_API_KEY": "gsk_x"})
    assert "GROQ_STT_MODEL=whisper" in out
    assert "GROQ_API_KEY=gsk_x" in out


def test_ends_with_single_newline():
    out = apply_env("A=1\n\n\n", {"B": "2"})
    assert out.endswith("\n") and not out.endswith("\n\n")
