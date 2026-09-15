"""Разовый перенос начала разговора из стенограммы claude в журнал.

Зачем. База поднялась не с первой реплики, а посреди работы: первые девять
обменов — предупреждение про VPN, постановка задачи на самодопил, заявка на
рефакторинг — в turns не попали вовсе. Для поиска их не существует, и на вопрос
«чего нельзя трогать на сервере» память отвечала мимо.

Стенограмма claude (~/.claude/projects/<путь>/<сессия>.jsonl) хранит всё с
метками времени, поэтому пробел закрывается точно, а не приблизительно.

Почему это скрипт, а не команда бота. Операция разовая и опасная в повторе:
второй прогон завёл бы дубли, по которым поиск начал бы возвращать одно и то же
дважды. Поэтому здесь есть сверка с уже записанным и режим --dry-run.

    venv/bin/python scripts/import_transcript.py <файл.jsonl> [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app.di import build_container            # noqa: E402
from app.domain.enums import TurnSource, TurnStatus  # noqa: E402
from app.domain.models import Turn            # noqa: E402
from app.domain.ports import TurnRepository   # noqa: E402
from app.services.journal import JournalService  # noqa: E402
from app.settings import Settings             # noqa: E402

# Служебные врезки, которые в стенограмме выглядят как реплики человека, но ими
# не являются: напоминания системы, вывод локальных команд, результаты
# инструментов. Их перенос засорил бы поиск текстом, которого никто не говорил.
SERVICE_MARKERS = (
    "<system-reminder", "<command-name>", "<local-command-stdout>",
    "<local-command-caveat>", "Caveat: The messages below",
    "<task-notification>", "Base directory for this skill:",
    "This session is being continued from a previous conversation",
)

# Строки, которыми среда продолжает прерванный ход. Человек их не писал.
SERVICE_EXACT = ("Continue from where you left off.",)

# Врезки, которые бот дописывает перед текстом: отметка времени и предупреждение
# о голосовом вводе. В журнал реплика попадает без них, поэтому и здесь их надо
# снять — иначе сверка с уже записанным не совпадёт ни разу и всё продублируется.
_PREFIX = re.compile(r"^\s*\[[^\]]{0,400}\]\s*", re.DOTALL)


def strip_prefixes(text: str) -> str:
    """Убрать ведущие служебные врезки в квадратных скобках, сколько бы их ни было."""
    previous = None
    while previous != text:
        previous = text
        text = _PREFIX.sub("", text, count=1)
    return text.strip()


def _text(record: dict) -> str:
    """Текст реплики. Блоки инструментов пропускаем — нужен только разговор."""
    message = record.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(block.get("text", "") for block in content
                     if isinstance(block, dict) and block.get("type") == "text")


def _moment(record: dict) -> datetime | None:
    raw = record.get("timestamp")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_exchanges(path: Path) -> list[dict]:
    """Собрать пары «реплика человека — что я ответил».

    Ответ склеивается из всех текстовых блоков до следующей реплики человека:
    один ход у меня обычно состоит из нескольких сообщений вперемешку с
    инструментами, а в журнале ход — это одна запись.
    """
    exchanges: list[dict] = []
    current: dict | None = None

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        kind, text = record.get("type"), _text(record).strip()
        if kind == "user":
            if not text or any(marker in text for marker in SERVICE_MARKERS):
                continue
            if text in SERVICE_EXACT:
                continue
            text = strip_prefixes(text)
            if not text:
                continue
            if current:
                exchanges.append(current)
            current = {"prompt": text, "answer": [], "when": _moment(record),
                       "session": record.get("sessionId")}
        elif kind == "assistant" and current and text:
            current["answer"].append(text)

    if current:
        exchanges.append(current)
    for item in exchanges:
        item["answer"] = "\n\n".join(item["answer"]).strip()
    return exchanges


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript", type=Path)
    parser.add_argument("--dry-run", action="store_true",
                        help="показать, что было бы перенесено, и ничего не писать")
    args = parser.parse_args()

    if not args.transcript.exists():
        print(f"нет файла: {args.transcript}")
        return 1

    settings = Settings()
    container = build_container()
    try:
        turns: TurnRepository = await container.get(TurnRepository)
        journal: JournalService = await container.get(JournalService)

        # Сверка по первым словам реплики: полное сравнение бесполезно, потому
        # что в базу текст мог попасть с отметкой времени или после правки.
        recorded = await turns.recent(1000)
        known = {strip_prefixes(t.prompt)[:80] for t in recorded}
        chat_id = next(iter(settings.allowed_user_ids), 0)

        # Граница: всё, что позже последнего записанного хода, — это текущий,
        # ещё не дописанный обмен. Он попадёт в журнал сам, когда завершится, и
        # перенос сделал бы из него дубль.
        cutoff = max((t.created_at for t in recorded if t.created_at), default=None)

        new = [ex for ex in read_exchanges(args.transcript)
               if ex["prompt"][:80] not in known and ex["when"] and ex["answer"]
               and (cutoff is None or ex["when"] < cutoff)]
        if not new:
            print("переносить нечего: всё уже в журнале")
            return 0

        print(f"нашёл {len(new)} обменов, которых нет в журнале:")
        for ex in new:
            when = ex["when"].astimezone(settings.tz).strftime("%d.%m %H:%M")
            print(f"  {when}  {ex['prompt'][:70]!r}  (ответ {len(ex['answer'])} симв.)")
        if args.dry_run:
            print("\n--dry-run: ничего не записано")
            return 0

        for ex in new:
            await turns.add(Turn(
                chat_id=chat_id, user_id=chat_id or None,
                prompt=ex["prompt"], answer=ex["answer"],
                status=TurnStatus.OK, source=TurnSource.TEXT,
                session_id=ex["session"], created_at=ex["when"],
                # Расход по этим ходам неизвестен: он остался в стенограмме
                # только суммарно. Нули честнее выдуманных чисел, но статистику
                # стоимости они занижают — об этом стоит помнить.
                tokens_in=0, tokens_out=0, cost_usd=0.0, duration_s=0.0,
            ))
        print(f"перенесено ходов: {len(new)}")

        made = 0
        while (done := await journal.index_pending(200)):
            made += done
            print(f"  индексация +{done}")
        print(f"обработано кусков и векторов: {made}")
        return 0
    finally:
        await container.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
