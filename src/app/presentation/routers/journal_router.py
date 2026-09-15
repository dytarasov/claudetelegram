"""Память: поиск по прошлым разговорам, заметки, сводка.

Это вспомогательные команды для меня самого. Контекст сессии рано или поздно
сворачивается, и без журнала прошлое исчезает бесследно — а вопросы вроде
«когда я это чинил и чем кончилось» возникают постоянно.
"""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from dishka.integrations.aiogram import FromDishka

from ...infrastructure.telegram.formatting import esc
from ...infrastructure.telegram.render import fmt_tokens
from ...infrastructure.telegram.sender import MessageSender
from ...services.journal import JournalService
from ...services.terms import TermsService

router = Router(name="journal")


@router.message(Command("recall"))
async def cmd_recall(message: Message, command: CommandObject,
                     journal: FromDishka[JournalService],
                     sender: FromDishka[MessageSender]) -> None:
    query = (command.args or "").strip()
    try:
        entries = await journal.recall(query, 8)
    except Exception as exc:  # noqa: BLE001
        await sender.send(message.chat.id, f"<b>Журнал недоступен</b>: <code>{esc(str(exc))}</code>")
        return
    if not entries:
        await sender.send(message.chat.id, f"Ничего не нашёл по «{esc(query)}».")
        return
    lines = [f"<b>Нашёл в истории</b>: {esc(query) or 'последнее'}", ""]
    for e in entries:
        lines.append(f"<i>{e.when.astimezone():%d.%m %H:%M}</i>  <b>{esc(e.prompt[:120])}</b>")
        # Показываем совпавший кусок, а не начало ответа: искали ведь место в
        # разговоре, а не факт, что разговор был.
        lines.append(f"{esc(e.passage or e.answer_head)}\n")
    await sender.send(message.chat.id, "\n".join(lines))


@router.message(Command("note"))
async def cmd_note(message: Message, command: CommandObject,
                   journal: FromDishka[JournalService],
                   sender: FromDishka[MessageSender]) -> None:
    text = (command.args or "").strip()
    if not text:
        await sender.send(message.chat.id,
                          "Что записать? <code>/note сторожа не трогать без нужды</code>")
        return
    note = await journal.add_note(text, source="user")
    await sender.send(message.chat.id, f"Записал <code>#{note.id}</code>.")


@router.message(Command("notes"))
async def cmd_notes(message: Message, command: CommandObject,
                    journal: FromDishka[JournalService],
                    sender: FromDishka[MessageSender]) -> None:
    found = await journal.find_notes((command.args or "").strip(), 10)
    if not found:
        await sender.send(message.chat.id, "Заметок нет.")
        return
    lines = ["<b>Заметки</b>", ""]
    for n in found:
        when = f"{n.created_at.astimezone():%d.%m}" if n.created_at else ""
        lines.append(f"<code>#{n.id}</code> <i>{when}</i> {esc(n.text)}")
    await sender.send(message.chat.id, "\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(message: Message, journal: FromDishka[JournalService],
                    sender: FromDishka[MessageSender]) -> None:
    try:
        st = await journal.stats()
    except Exception as exc:  # noqa: BLE001
        await sender.send(message.chat.id, f"<b>Журнал недоступен</b>: <code>{esc(str(exc))}</code>")
        return
    since = st.get("since")
    await sender.send(message.chat.id, "\n".join([
        "<b>Сводка</b>",
        f"ходов: {st.get('turns', 0)}",
        f"токенов: {fmt_tokens(int(st.get('tokens') or 0))}",
        f"потрачено: ${float(st.get('cost') or 0):.2f}",
        f"считаю с: {since:%d.%m.%Y}" if since else "",
    ]).strip())


@router.message(Command("memory"))
async def cmd_memory(message: Message, journal: FromDishka[JournalService],
                     sender: FromDishka[MessageSender]) -> None:
    """Что с векторной памятью: включена ли и насколько проиндексирована."""
    stats = await journal.memory_stats()
    if not stats["available"]:
        await sender.send(
            message.chat.id,
            "<b>Векторная память</b>: выключена\n"
            "<i>нужен ключ эмбеддера в .env (EMBEDDINGS_API_KEY)</i>\n\n"
            "Поиск по журналу работает полнотекстом.",
        )
        return
    total, indexed = stats["total"], stats["indexed"]
    percent = f"{indexed / total * 100:.0f}%" if total else "—"
    await sender.send(message.chat.id, "\n".join([
        "<b>Векторная память</b>",
        f"модель: <code>{esc(stats['model'])}</code> · vector({stats['dim']})",
        f"проиндексировано: {indexed} из {total} ({percent})",
        "",
        "<i>/recall ищет и по смыслу, и по словам одновременно</i>",
    ]))


@router.message(Command("terms"))
async def cmd_terms(message: Message, terms: FromDishka[TermsService],
                    sender: FromDishka[MessageSender]) -> None:
    """Показать словарь расшифровки: что во что превращается."""
    mapping = await terms.mapping()
    lines = [f"<b>Словарь расшифровки</b> — {len(mapping)} терминов", ""]
    for canonical, variants in sorted(mapping.items()):
        lines.append(f"<b>{esc(canonical)}</b> ← <i>{esc(', '.join(variants))}</i>")
    lines.append("")
    lines.append("Добавить: <code>/term Claude Code = клауди код, кладкод</code>")
    await sender.send(message.chat.id, "\n".join(lines))


@router.message(Command("term"))
async def cmd_term(message: Message, command: CommandObject,
                   terms: FromDishka[TermsService],
                   sender: FromDishka[MessageSender]) -> None:
    """Научить словарь новому слову: /term <как надо> = <как услышалось>, …"""
    raw = (command.args or "").strip()
    if "=" not in raw:
        await sender.send(
            message.chat.id,
            "Как пользоваться:\n<code>/term pgvector = пеговектор, пего вектор</code>\n"
            "Посмотреть весь словарь — /terms",
        )
        return
    canonical, _, variants_raw = raw.partition("=")
    variants = [v.strip() for v in variants_raw.split(",") if v.strip()]
    if not canonical.strip() or not variants:
        await sender.send(message.chat.id, "Нужно и слово, и хотя бы один вариант слева от «=».")
        return
    saved = await terms.learn(canonical.strip(), variants, source="user")
    await sender.send(
        message.chat.id,
        f"Запомнил: <i>{esc(', '.join(t.variant for t in saved))}</i> → "
        f"<b>{esc(canonical.strip())}</b>\n<i>действует со следующего голосового</i>",
    )
