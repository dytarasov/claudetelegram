"""Инструменты памяти, которыми пользуюсь я сам через MCP.

Проверяется не база, а то, что ломалось на живом прогоне:

  • дата события уезжала на сутки назад, потому что Postgres отдаёт timestamptz
    в UTC, а форматировалась она без перевода в пояс человека;
  • поиск по фактам возвращал пусто, потому что websearch_to_tsquery склеивает
    слова через AND, и одно лишнее слово в вопросе обнуляло выдачу;
  • ошибка внутри инструмента должна приходить моделью читаемым текстом, а не
    рвать соединение по протоколу — иначе вызов нельзя исправить со второй
    попытки.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.domain.models import Event
from app.infrastructure.repositories.base import or_terms
from app.mcp_server import MemoryTools, Server

MSK = ZoneInfo("Europe/Moscow")


# --- дата события ---------------------------------------------------------- #
def test_event_date_is_shown_in_users_timezone():
    """00:00 первого сентября по Москве — это 21:00 тридцать первого в UTC.

    Без перевода обратно человек читает «31.08» про то, что записал на «01.09».
    """
    stored = datetime(2026, 8, 31, 21, 0, tzinfo=timezone.utc)
    event = Event(title="отдать деньги", happened_at=stored, date_precision="day")
    assert event.when_text(MSK) == "01.09.2026"
    assert event.when_text() == "31.08.2026"   # то самое расхождение


def test_precision_does_not_invent_hours():
    stored = datetime(2026, 3, 15, 12, 0, tzinfo=MSK)
    assert Event(title="x", happened_at=stored, date_precision="month").when_text(MSK) == "03.2026"
    assert Event(title="x", happened_at=stored, date_precision="year").when_text(MSK) == "2026"


# --- поиск ------------------------------------------------------------------ #
def test_query_terms_are_joined_by_or():
    assert or_terms("чего нельзя трогать") == "чего OR нельзя OR трогать"
    assert or_terms("  ") == "  "        # пустой запрос не превращается в мусор
    assert or_terms("vpn") == "vpn"


# --- разбор аргументов от модели -------------------------------------------- #
class FakeMemory:
    def __init__(self):
        self.written = []

    async def remember_event(self, title, happened_at, **kw):
        self.written.append((title, happened_at))
        return Event(title=title, happened_at=happened_at, id=1,
                     date_precision=kw.get("date_precision", "minute"))

    async def profile(self, subject=None):
        return {"facts": [], "subject": subject}


class FakeSettings:
    tz = MSK


def _tools() -> tuple[MemoryTools, FakeMemory]:
    memory = FakeMemory()
    return MemoryTools(memory, FakeSettings()), memory


@pytest.mark.asyncio
async def test_naive_date_is_read_in_users_timezone():
    """Модель думает днём человека: «15:00» — это три часа дня у него."""
    tools, memory = _tools()
    await tools.call("remember", {"kind": "event", "text": "встреча",
                                  "when": "2026-09-01T15:00"})
    _title, when = memory.written[0]
    assert when == datetime(2026, 9, 1, 15, 0, tzinfo=MSK)


@pytest.mark.asyncio
async def test_bad_date_explains_itself_instead_of_crashing():
    tools, _ = _tools()
    with pytest.raises(ValueError) as err:
        await tools.call("remember", {"kind": "event", "text": "x", "when": "вчера"})
    assert "ISO" in str(err.value)


@pytest.mark.asyncio
async def test_empty_text_is_refused():
    tools, _ = _tools()
    with pytest.raises(ValueError):
        await tools.call("remember", {"kind": "fact", "text": "   "})


# --- протокол ---------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_handshake_answers_with_clients_protocol_version():
    """Своя зашитая версия означала бы отказ работать с новым claude из-за пустяка."""
    server = Server(_tools()[0])
    reply = await server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2099-01-01"}})
    assert reply["result"]["protocolVersion"] == "2099-01-01"
    assert reply["result"]["serverInfo"]["name"] == "memory"


@pytest.mark.asyncio
async def test_notifications_get_no_answer():
    """Ответ на уведомление — нарушение JSON-RPC, клиент от него отваливается."""
    server = Server(_tools()[0])
    assert await server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


@pytest.mark.asyncio
async def test_tool_failure_comes_back_as_text_not_as_broken_connection():
    server = Server(_tools()[0])
    reply = await server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                 "params": {"name": "remember",
                                            "arguments": {"kind": "event", "text": "x"}}})
    assert "error" not in reply
    assert reply["result"]["isError"] is True
    assert "when" in reply["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_every_tool_is_callable_and_described():
    """Инструмент без обработчика виден в списке, но падает при вызове."""
    from app.mcp_server import TOOLS
    tools, _ = _tools()
    for tool in TOOLS:
        assert tool["description"].strip(), tool["name"]
        assert hasattr(tools, f"_{tool['name']}"), tool["name"]


@pytest.mark.asyncio
async def test_result_is_valid_json_for_the_model():
    server = Server(_tools()[0])
    reply = await server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                 "params": {"name": "profile", "arguments": {}}})
    json.loads(reply["result"]["content"][0]["text"])
