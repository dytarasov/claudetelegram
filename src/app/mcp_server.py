"""MCP-сервер памяти — то, чем я пользуюсь изнутри разговора.

Зачем он вообще. Слои памяти лежали в базе, но добраться до них из хода мысли
было нечем: поиск висел на телеграм-команде, которую набирает человек. Значит,
пользоваться памятью мог кто угодно, кроме того, кому она нужна. MCP закрывает
именно это: инструменты появляются прямо в моём наборе, рядом с чтением файлов,
и я обращаюсь к ним сам, когда понимаю, что чего-то не помню.

Почему не автоподстановка профиля в каждый промпт. Так было бы проще, но хуже:
фиксированная врезка тратит токены на каждом ходу независимо от темы и всё
равно не покрывает случай «а когда мы это решали». Инструмент вызывается тогда,
когда нужен, и может задать уточняющий запрос — врезка не может.

Почему протокол написан руками, а не взят библиотекой. MCP поверх stdio — это
JSON-RPC 2.0 построчно: initialize, tools/list, tools/call. Здесь это сотня
строк без зависимостей, которые нечему ломать при обновлении пакета, а лишняя
зависимость в проекте, который сам себя выкатывает, стоит дороже экономии.

Жёсткое правило этого файла: в stdout не попадает ничего, кроме ответов
протокола. Любая отладка — только в stderr, иначе клиент видит мусор вместо
JSON и обрывает соединение.

Запускается не человеком, а самим claude по конфигу run/mcp.json.
"""
from __future__ import annotations

import asyncio
import json
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.di import build_container            # noqa: E402
from app.services.memory import MemoryService  # noqa: E402
from app.settings import Settings              # noqa: E402

# Версия протокола: отвечаем той, которую попросил клиент. Своя жёстко зашитая
# строка означала бы отказ работать с более новым claude из-за пустяка.
FALLBACK_PROTOCOL = "2025-06-18"
SERVER_INFO = {"name": "memory", "version": "1.0.0"}


def log(message: str) -> None:
    print(f"[memory-mcp] {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
#  Описание инструментов
# --------------------------------------------------------------------------- #
# Описания написаны для читателя-модели, то есть для меня: в них сказано не
# «что делает функция», а «когда её звать». Инструмент, про который непонятно,
# в какой момент он полезен, не будет вызван ни разу.
TOOLS = [
    {
        "name": "profile",
        "description": (
            "Что известно о человеке всегда: закреплённые факты, правила и запреты, "
            "открытые обязательства, текущие дата и время. Звать в начале работы над "
            "чем-то содержательным и всегда, когда собираешься тронуть систему: "
            "там лежат запреты вроде «этот процесс не трогать»."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string",
                            "description": "сузить до одного субъекта: user, server, имя человека"},
            },
        },
    },
    {
        "name": "recall",
        "description": (
            "Поиск по всей памяти сразу: факты, события, сырые реплики разговора, заметки. "
            "Звать, когда человек ссылается на прошлое («как мы договаривались», «ты уже "
            "чинил это»), когда не хватает подробности после сворачивания контекста, и "
            "перед тем как переспрашивать то, что уже могло быть сказано. По репликам "
            "работает смысловой поиск, поэтому запрос можно формулировать своими словами."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "о чём вспомнить, обычными словами"},
                "kind": {"type": "string", "enum": ["all", "facts", "events", "turns", "notes"],
                         "description": "по умолчанию all — искать во всех слоях"},
                "limit": {"type": "integer", "description": "сколько записей на слой, по умолчанию 8"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "remember",
        "description": (
            "Записать в память. Три вида. fact — устойчивое утверждение о человеке или "
            "системе; если задать key, новая запись заменит прежнюю с тем же ключом, а "
            "старая уйдёт в историю. event — то, что случилось или предстоит, обязательно "
            "с датой. note — вывод по работе, «почему сделано так», «на что не наступать». "
            "Звать сразу, как узнал что-то, чего не будет в контексте завтра. Ставить "
            "pinned только тому, без чего нельзя работать: запреты, постоянные величины, "
            "резкие предпочтения."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["fact", "event", "note"]},
                "text": {"type": "string",
                         "description": "сам факт, название события или текст заметки"},
                "subject": {"type": "string",
                            "description": "факт: о ком/чём. user по умолчанию"},
                "key": {"type": "string",
                        "description": "факт: короткий ключ, задаёт тождество и замещение"},
                "fact_kind": {"type": "string",
                              "enum": ["fact", "constant", "preference", "rule"],
                              "description": "факт: rule — это запрет или требование"},
                "pinned": {"type": "boolean", "description": "факт: включить в профиль"},
                "confidence": {"type": "number",
                               "description": "факт: 1.0 сказано прямо, 0.5 выведено самим"},
                "when": {"type": "string",
                         "description": "событие: дата ISO, например 2026-09-01 или 2026-09-01T15:00"},
                "precision": {"type": "string", "enum": ["minute", "day", "month", "year"],
                              "description": "событие: насколько точно известно время"},
                "event_kind": {"type": "string",
                               "enum": ["event", "decision", "deadline", "meeting",
                                        "payment", "trip"]},
                "details": {"type": "string", "description": "событие: подробности"},
                "people": {"type": "array", "items": {"type": "string"},
                           "description": "событие: кто участвовал"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "from_user": {"type": "boolean",
                              "description": "человек сказал это прямо, а не выведено мной"},
            },
            "required": ["kind", "text"],
        },
    },
    {
        "name": "timeline",
        "description": (
            "События в окне времени. Звать на вопросы вида «что было на прошлой неделе», "
            "«что предстоит», «когда это случилось». Без параметров — месяц назад и "
            "неделя вперёд."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "дата ISO, начало окна"},
                "until": {"type": "string", "description": "дата ISO, конец окна"},
                "days_back": {"type": "integer", "description": "или: на сколько дней назад"},
                "days_ahead": {"type": "integer", "description": "и на сколько вперёд"},
            },
        },
    },
    {
        "name": "history",
        "description": (
            "Как менялся один факт: все версии ключа, свежие первыми. Звать, когда "
            "нужно понять, откуда взялось знание, или когда человек говорит, что "
            "раньше было иначе."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "key": {"type": "string"},
            },
            "required": ["key"],
        },
    },
    {
        "name": "forget",
        "description": (
            "Убрать запись, которая перестала быть правдой. Факт не стирается, а "
            "закрывается датой — старые реплики должны остаться понятными. Событие и "
            "заметка удаляются насовсем."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["fact", "event", "note"]},
                "id": {"type": "integer"},
            },
            "required": ["kind", "id"],
        },
    },
    {
        "name": "stats",
        "description": "Что вообще лежит в памяти и покрыт ли векторный индекс.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class MemoryTools:
    """Тонкая обёртка: разбор аргументов от модели и вызов сервиса.

    Вся защита от кривых аргументов живёт здесь, потому что аргументы приходят
    от языковой модели: она может прислать дату строкой в неожиданном формате
    или забыть обязательное поле. Падать в таких случаях нельзя — надо вернуть
    понятное объяснение, по которому вызов можно исправить со второй попытки.
    """

    def __init__(self, memory: MemoryService, settings: Settings) -> None:
        self._memory = memory
        self._tz = settings.tz

    async def call(self, name: str, args: dict) -> dict:
        handler = getattr(self, f"_{name}", None)
        if handler is None:
            raise ValueError(f"нет такого инструмента: {name}")
        return await handler(args)

    async def _profile(self, args: dict) -> dict:
        return await self._memory.profile(args.get("subject") or None)

    async def _recall(self, args: dict) -> dict:
        return await self._memory.recall(
            str(args.get("query", "")), args.get("kind", "all") or "all",
            int(args.get("limit") or 8))

    async def _remember(self, args: dict) -> dict:
        kind = args.get("kind")
        text = str(args.get("text", "")).strip()
        if not text:
            raise ValueError("нечего запоминать: пустой text")
        source = "user" if args.get("from_user") else "assistant"
        tags = [str(t) for t in (args.get("tags") or [])]

        if kind == "fact":
            fact = await self._memory.remember_fact(
                text, subject=str(args.get("subject") or "user"),
                key=args.get("key"), kind=str(args.get("fact_kind") or "fact"),
                pinned=bool(args.get("pinned")),
                confidence=float(args.get("confidence", 1.0)),
                tags=tags, source=source)
            return {"saved": "fact", "id": fact.id, "as": fact.label,
                    "pinned": fact.pinned}
        if kind == "event":
            when = self._when(args.get("when"))
            event = await self._memory.remember_event(
                text, when, details=args.get("details"),
                date_precision=str(args.get("precision") or "minute"),
                kind=str(args.get("event_kind") or "event"),
                people=[str(p) for p in (args.get("people") or [])],
                tags=tags, source=source)
            return {"saved": "event", "id": event.id,
                    "when": event.when_text(self._tz)}
        if kind == "note":
            note = await self._memory.add_note(text, tags)
            return {"saved": "note", "id": note.id}
        raise ValueError(f"неизвестный вид записи: {kind}")

    async def _timeline(self, args: dict) -> dict:
        now = datetime.now(self._tz)
        since = self._when(args.get("since")) if args.get("since") else \
            now - timedelta(days=int(args.get("days_back") or 30))
        until = self._when(args.get("until")) if args.get("until") else \
            now + timedelta(days=int(args.get("days_ahead") or 7))
        return await self._memory.timeline(since, until)

    async def _history(self, args: dict) -> dict:
        return await self._memory.history(str(args.get("subject") or "user"),
                                          str(args["key"]))

    async def _forget(self, args: dict) -> dict:
        kind, row_id = args.get("kind"), int(args["id"])
        if kind == "fact":
            return {"retired": await self._memory.retire_fact(row_id)}
        if kind == "event":
            return {"deleted": await self._memory.forget_event(row_id)}
        if kind == "note":
            return {"deleted": await self._memory._journal.drop_note(row_id)}
        raise ValueError(f"неизвестный вид записи: {kind}")

    async def _stats(self, _args: dict) -> dict:
        return await self._memory.stats()

    def _when(self, raw) -> datetime:
        """Дата от модели. Принимаем и «2026-09-01», и полный ISO.

        Наивное время трактуем в часовом поясе человека, а не сервера: модель
        думает его днём, и «15:00» значит три часа дня у него.
        """
        if not raw:
            raise ValueError("для события нужна дата в поле when")
        text = str(raw).strip().replace("/", "-")
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"не разобрал дату «{raw}», нужен ISO: 2026-09-01 или "
                             f"2026-09-01T15:00") from exc
        return moment if moment.tzinfo else moment.replace(tzinfo=self._tz)


# --------------------------------------------------------------------------- #
#  Протокол
# --------------------------------------------------------------------------- #
class Server:
    def __init__(self, tools: MemoryTools) -> None:
        self._tools = tools
        self._protocol = FALLBACK_PROTOCOL

    async def handle(self, message: dict) -> dict | None:
        """Один запрос. None означает уведомление, на которое отвечать нельзя."""
        method = message.get("method")
        msg_id = message.get("id")
        if msg_id is None:              # уведомление (notifications/initialized и т.п.)
            return None

        try:
            result = await self._dispatch(method, message.get("params") or {})
        except Exception as exc:        # noqa: BLE001 — падение сервера дороже любой ошибки
            log(f"ошибка в {method}: {exc}\n{traceback.format_exc()}")
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32603, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    async def _dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            self._protocol = params.get("protocolVersion") or FALLBACK_PROTOCOL
            return {"protocolVersion": self._protocol,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return await self._call(params)
        if method == "ping":
            return {}
        raise ValueError(f"метод не поддерживается: {method}")

    async def _call(self, params: dict) -> dict:
        name = params.get("name", "")
        try:
            payload = await self._tools.call(name, params.get("arguments") or {})
            text = json.dumps(payload, ensure_ascii=False, indent=1, default=str)
            return {"content": [{"type": "text", "text": text}]}
        except Exception as exc:  # noqa: BLE001
            # Ошибку инструмента возвращаем как результат с isError, а не как
            # ошибку протокола: так модель видит текст и может исправить вызов,
            # вместо того чтобы получить обрыв связи.
            log(f"инструмент {name} не отработал: {exc}")
            return {"content": [{"type": "text", "text": f"не вышло: {exc}"}],
                    "isError": True}


async def serve() -> None:
    settings = Settings()
    container = build_container()
    memory = await container.get(MemoryService)
    server = Server(MemoryTools(memory, settings))
    log("готов")

    reader = asyncio.StreamReader(limit=8 * 1024 * 1024)
    await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

    try:
        while line := await reader.readline():
            raw = line.decode("utf-8", "replace").strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                log(f"не разобрал строку: {raw[:200]}")
                continue
            response = await server.handle(message)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
    finally:
        await container.close()
        log("закончил")


if __name__ == "__main__":
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
