"""Отрисовка — чистые функции, поэтому проверяются без всякого окружения."""
from __future__ import annotations

from app.infrastructure.telegram.render import (PACK_LIMIT, fmt_duration, fmt_tokens,
                                                pack, short_path, short_url, shorten,
                                                tool_html)


def test_shorten_keeps_short_text():
    assert shorten("привет", 10) == "привет"


def test_shorten_collapses_whitespace_and_cuts():
    assert shorten("а  б\n\nв", 100) == "а б в"
    assert shorten("абвгде", 4) == "абв…"


def test_fmt_tokens():
    assert fmt_tokens(999) == "999"
    assert fmt_tokens(1500) == "1.5k"
    assert fmt_tokens(2_000_000) == "2.0M"


def test_fmt_duration():
    assert fmt_duration(42) == "42с"
    assert fmt_duration(75) == "1м 15с"
    assert fmt_duration(3700) == "1ч 01м"


def test_short_path_collapses_deep_paths_keeps_shallow():
    assert short_path("/root/tgassistant/src/app/telegram/render.py") == "…/telegram/render.py"
    assert short_path("/etc/hosts") == "/etc/hosts"  # ≤2 сегмента — не трогаем
    assert short_path("file.py") == "file.py"


def test_short_url_drops_scheme_and_query():
    assert short_url("https://procycle.us/model/honda?x=1") == "procycle.us/model/honda"


def test_tool_html_shows_the_useful_field():
    assert "ls -la" in tool_html("Bash", {"command": "ls -la"})
    assert "/etc/hosts" in tool_html("Read", {"file_path": "/etc/hosts"})


def test_tool_html_collapses_long_file_path():
    out = tool_html("Edit", {"file_path": "/root/tgassistant/src/app/telegram/render.py"})
    assert "…/telegram/render.py" in out
    assert "/root/tgassistant" not in out


def test_tool_html_escapes_html():
    assert "<script>" not in tool_html("Bash", {"command": "<script>"})


def test_pack_merges_small_pieces():
    assert pack(["раз", "два"]) == ["раз\n\nдва"]


def test_pack_splits_when_over_limit():
    piece = "х" * (PACK_LIMIT - 10)
    out = pack([piece, piece])
    assert len(out) == 2


def test_pack_skips_empty():
    assert pack(["", "текст", ""]) == ["текст"]
