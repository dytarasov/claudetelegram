"""Мастер установки: проверяем чистую логику записи .env, без Телеграма.

apply_env — единственное место, где мастер меняет конфиг, поэтому и покрыто:
существующие ключи должны переписываться на месте (комментарии и порядок целы),
недостающие — дописываться, а секреты пользователя не должны затираться зря.
"""
from __future__ import annotations

from app.claude_login import extract_url
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


# --- разбор ссылки входа claude (из вывода setup-token) --------------------- #
URL = ("https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b"
       "&response_type=code&scope=user%3Ainference&code_challenge=7s6WtUNJ&state=Jofb6")


def test_extract_url_from_plain_output():
    assert extract_url(f"Browser didn't open? Use the url below\n{URL}\nPaste code") == URL


def test_extract_url_survives_ansi_and_stops_at_space():
    noisy = f"\x1b[1m\x1b[32m{URL}\x1b[0m  Paste code here if prompted >"
    assert extract_url(noisy) == URL


def test_extract_url_none_when_absent():
    assert extract_url("Welcome to Claude Code\nPaste code here >") is None
