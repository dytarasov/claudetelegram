"""Markdown Claude'а -> HTML, который переваривает Telegram, плюс безопасная нарезка.

Telegram поддерживает лишь горстку тегов (b, i, u, s, code, pre, a, blockquote),
любой другой markdown надо либо перевести, либо обезвредить.
"""
from __future__ import annotations

import html
import re

TG_LIMIT = 3900  # с запасом от жёстких 4096

_FENCE_OPEN = re.compile(r"^\s*(`{3,})(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")


def esc(text: str) -> str:
    return html.escape(str(text), quote=False)


# --------------------------------------------------------------------------- #
#  markdown -> html
# --------------------------------------------------------------------------- #
def _inline(text: str) -> str:
    """Инлайновая разметка. Сначала прячем `code`, чтобы не жевать его содержимое."""
    spans: list[str] = []

    def stash(m: re.Match) -> str:
        spans.append(m.group(1))
        return f"\x00{len(spans) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", stash, text)
    text = esc(text)

    # Заголовки -> жирная строка.
    text = re.sub(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", r"<b>\1</b>", text, flags=re.M)
    # Горизонтальная линейка.
    text = re.sub(r"^\s{0,3}([-*_])\s*(?:\1\s*){2,}$", "──────────", text, flags=re.M)
    # Маркеры списка -> буллет (звёздочку убираем до курсива, иначе она его ломает).
    text = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", text, flags=re.M)

    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", text, flags=re.S)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.S)
    text = re.sub(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w_])_([^_\n]+?)_(?![\w_])", r"<i>\1</i>", text)
    # Ссылки: [текст](url)
    text = re.sub(
        r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text
    )

    def unstash(m: re.Match) -> str:
        return f"<code>{esc(spans[int(m.group(1))])}</code>"

    return re.sub(r"\x00(\d+)\x00", unstash, text)


def _text_block(text: str) -> str:
    """Обычный текст: таблицы уводим в <pre>, остальное — в инлайновую разметку."""
    out: list[str] = []
    buf: list[str] = []
    table: list[str] = []

    def flush_text() -> None:
        if buf:
            out.append(_inline("\n".join(buf)))
            buf.clear()

    def flush_table() -> None:
        if table:
            out.append("<pre>" + esc("\n".join(table)) + "</pre>")
            table.clear()

    for line in text.split("\n"):
        if _TABLE_ROW.match(line):
            flush_text()
            table.append(line.strip())
        else:
            flush_table()
            buf.append(line)
    flush_text()
    flush_table()
    return "\n".join(out)


def md_to_html(md: str) -> str:
    """Переводит markdown в Telegram-HTML."""
    parts: list[str] = []
    pos = 0
    for m in re.finditer(r"```([^\n`]*)\n?(.*?)(?:^```|\Z)", md, re.S | re.M):
        parts.append(_text_block(md[pos : m.start()]))
        lang = m.group(1).strip()
        body = m.group(2).rstrip("\n")
        attr = f' class="language-{esc(lang)}"' if lang else ""
        parts.append(f"<pre><code{attr}>{esc(body)}</code></pre>")
        pos = m.end()
    parts.append(_text_block(md[pos:]))
    return "".join(parts).strip()


# --------------------------------------------------------------------------- #
#  нарезка
# --------------------------------------------------------------------------- #
def split_markdown(md: str, limit: int = TG_LIMIT) -> list[str]:
    """Режет markdown на куски <= limit, не разрывая ``` блоки.

    Если блок кода не влезает — он закрывается в конце куска и переоткрывается
    в следующем с тем же языком, так что подсветка не разъезжается.
    """
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    fence: str | None = None  # активный ```-маркер
    lang = ""

    def flush() -> None:
        nonlocal cur, size
        if fence:
            cur.append(fence)
        text = "\n".join(cur).strip()
        if text:
            chunks.append(text)
        cur = [f"{fence}{lang}"] if fence else []
        size = sum(len(x) + 1 for x in cur)

    for line in md.split("\n"):
        # Слишком длинная одиночная строка — рубим жёстко.
        pieces = (
            [line]
            if len(line) <= limit - 200
            else [line[i : i + limit - 200] for i in range(0, len(line), limit - 200)]
        )
        for piece in pieces:
            if size + len(piece) + 1 > limit and cur:
                flush()
            cur.append(piece)
            size += len(piece) + 1

            m = _FENCE_OPEN.match(piece)
            if m:
                if fence is None:
                    fence, lang = m.group(1), m.group(2).strip()
                elif piece.strip().startswith(fence):
                    fence, lang = None, ""

    if fence:
        cur.append(fence)
    text = "\n".join(cur).strip()
    if text:
        chunks.append(text)
    return chunks or [""]


TG_HARD_LIMIT = 4096


def to_telegram_chunks(md: str, limit: int = TG_LIMIT) -> list[str]:
    """markdown -> список готовых к отправке HTML-кусков.

    HTML длиннее исходника (экранирование и теги), поэтому режем с запасом и,
    если кусок всё же перерос жёсткий лимит Telegram, пересобираем помельче.
    """
    for lim in (limit, limit // 2, limit // 3, limit // 6):
        chunks = [md_to_html(c) for c in split_markdown(md, lim)]
        if all(len(c) <= TG_HARD_LIMIT for c in chunks):
            return chunks
    # Патология (сплошь спецсимволы) — отдаём голым текстом фиксированными кусками.
    flat = esc(md)
    step = TG_HARD_LIMIT - 100
    return [flat[i : i + step] for i in range(0, len(flat), step)] or [""]


def tail(text: str, limit: int) -> str:
    """Хвост текста для «живого» сообщения — режем по границе строки."""
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    nl = cut.find("\n")
    return "…\n" + (cut[nl + 1 :] if 0 <= nl < 200 else cut)
