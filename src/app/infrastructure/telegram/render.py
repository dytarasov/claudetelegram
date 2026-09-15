"""Чистые функции отрисовки: данные → HTML для Telegram.

Ни одной сетевой операции, ни одного глобального объекта — поэтому всё это
проверяется обычными юнит-тестами и переиспользуется где угодно.
"""
from __future__ import annotations

import json

from .formatting import TG_HARD_LIMIT, esc

# Сколько последних инструментов показывать в «живом» сообщении: больше —
# и сообщение начинает прыгать, а Telegram ругается на частые правки.
MAX_LIVE_TOOLS = 6
LIVE_TEXT_TAIL = 1200
PACK_LIMIT = 3900

# Подписи инструментов вместо иконок: строка «bash» читается однозначно, а
# набор картинок приходится расшифровывать каждый раз заново.
TOOL_LABELS = {
    "Bash": "bash", "Read": "read", "Write": "write", "Edit": "edit",
    "NotebookEdit": "notebook", "Glob": "glob", "Grep": "grep",
    "WebFetch": "fetch", "WebSearch": "search", "Task": "agent",
    "TodoWrite": "todo", "Skill": "skill", "Monitor": "monitor",
}
# По этому префиксу отличаем строку инструмента от текста ответа.
TOOL_MARK = "→"


def shorten(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def short_path(path: str) -> str:
    """Схлопнуть длинный путь до «…/родитель/файл».

    Абсолютные пути в репозитории тянутся на полстроки и читать в них нечего,
    кроме хвоста. Короткие (два сегмента и меньше) оставляем как есть — там
    обрезать нечего, а начальный слэш иногда важен.
    """
    parts = [p for p in str(path).split("/") if p]
    if len(parts) > 2:
        return "…/" + "/".join(parts[-2:])
    return path


def short_url(url: str) -> str:
    """Домен плюс усечённый хвост — без схемы и мусора запроса."""
    u = str(url).split("://", 1)[-1]
    u = u.split("?", 1)[0].rstrip("/")
    return u


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def fmt_duration(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec}с"
    if sec < 3600:
        return f"{sec // 60}м {sec % 60:02d}с"
    return f"{sec // 3600}ч {(sec % 3600) // 60:02d}м"


def tool_html(name: str, inp: dict) -> str:
    """Одна строка про вызов инструмента: иконка, имя и самое важное поле.

    Показываем именно то, что человек хочет увидеть: для Bash — команду, для
    Read — путь. Полный json был бы нечитаем и раздул бы сообщение.
    """
    label = TOOL_LABELS.get(name, name.lower())
    if name == "Bash":
        detail = inp.get("command", "")
    elif name in ("Read", "Write", "Edit", "NotebookEdit"):
        detail = short_path(inp.get("file_path", ""))
    elif name in ("Glob", "Grep"):
        detail = inp.get("pattern", "")
        if inp.get("path"):
            detail += f"  ·  {short_path(inp['path'])}"
    elif name == "WebFetch":
        detail = short_url(inp.get("url", ""))
    elif name == "WebSearch":
        detail = inp.get("query", "")
    elif name == "Task":
        detail = inp.get("description", "")
    elif name == "TodoWrite":
        detail = f"{len(inp.get('todos', []))} пунктов"
    elif name == "Skill":
        detail = inp.get("skill", "")
    else:
        detail = json.dumps(inp, ensure_ascii=False)
    detail = shorten(detail, 120)
    body = f"<code>{esc(detail)}</code>" if detail else ""
    return f"{TOOL_MARK} <b>{esc(label)}</b> {body}".rstrip()


def pack(pieces: list[str], limit: int = PACK_LIMIT) -> list[str]:
    """Склеить готовые HTML-куски в сообщения, не превышая лимит Telegram."""
    out: list[str] = []
    cur = ""
    for piece in pieces:
        if not piece:
            continue
        if len(piece) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(piece[:TG_HARD_LIMIT])
            continue
        candidate = f"{cur}\n\n{piece}" if cur else piece
        if len(candidate) > limit:
            out.append(cur)
            cur = piece
        else:
            cur = candidate
    if cur:
        out.append(cur)
    return out
