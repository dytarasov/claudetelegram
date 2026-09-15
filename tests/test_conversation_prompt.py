"""Как реплика подаётся модели в зависимости от происхождения.

Напечатанный текст должен доезжать слово в слово: любая служебная обвязка в нём
— это шум, который человек не просил. Наговоренное, наоборот, доезжает с
пометкой о происхождении и списком уже сделанных замен, чтобы модель могла их
отменить, если словарь ошибся.
"""
from __future__ import annotations

from app.domain.dto import TurnRequest
from app.domain.enums import TurnSource
from app.services.conversation import VOICE_HINT_ADD, VOICE_PREAMBLE, ConversationService
from app.services.terms import TermsService

from test_terms import FakeTermRepository


def _service(terms: TermsService | None = None) -> ConversationService:
    # Для сборки реплики нужен только словарь: процесс, отправка и журнал в этом
    # пути не участвуют, поэтому и подсовывать их незачем.
    return ConversationService(
        process=None, notifier=None, presenters=None, turns=None, kv=None,
        terms=terms or TermsService(FakeTermRepository()), settings=None,
    )


async def _prompt(text: str, source: TurnSource, service=None) -> str:
    return await (service or _service())._prompt_for_model(
        TurnRequest(chat_id=1, text=text, source=source)
    )


async def test_typed_text_is_not_rewritten():
    """Текст остаётся дословным: перед ним только отметка времени, и всё."""
    out = await _prompt("перезапусти сервис", TurnSource.TEXT)
    assert out.endswith("\n\nперезапусти сервис")
    assert "словарю" not in out


async def test_file_source_is_not_decorated():
    """У файлов пояснение уже внутри текста («Я прислал файл: …»)."""
    out = await _prompt("Я прислал файл: /tmp/x", TurnSource.FILE)
    assert out.endswith("\n\nЯ прислал файл: /tmp/x")
    assert VOICE_PREAMBLE not in out


async def test_voice_gets_preamble_and_text():
    out = await _prompt("перезапусти сервис", TurnSource.VOICE)
    assert VOICE_PREAMBLE in out
    assert out.endswith("перезапусти сервис")


async def test_voice_text_is_corrected_by_dictionary():
    out = await _prompt("поставь пайтон", TurnSource.VOICE)
    assert out.endswith("поставь Python")


async def test_model_is_told_what_was_replaced():
    out = await _prompt("поставь пайтон", TurnSource.VOICE)
    assert "пайтон → Python" in out
    assert "если подмена неуместна" in out


async def test_no_replacement_note_when_nothing_changed():
    out = await _prompt("перезапусти сервис", TurnSource.VOICE)
    assert "По словарю уже поправлено" not in out


async def test_model_is_told_how_to_extend_the_dictionary():
    out = await _prompt("что-нибудь", TurnSource.VOICE)
    assert VOICE_HINT_ADD.strip() in out
    assert "cli.py term add" in out


async def test_preamble_tells_the_model_to_ask_when_unclear():
    assert "переспроси" in VOICE_PREAMBLE


async def test_every_turn_carries_the_current_time():
    """Без отметки времени «вчера» и «на прошлой неделе» не с чем соотнести."""
    out = await _prompt("что вчера сломалось?", TurnSource.TEXT)
    assert out.startswith("[сейчас ")
    assert out.endswith("что вчера сломалось?")


async def test_gap_since_previous_turn_is_reported():
    from datetime import datetime, timedelta

    service = _service()
    service._last_turn_at = datetime.now().astimezone() - timedelta(days=3)
    out = await service._prompt_for_model(
        TurnRequest(chat_id=1, text="привет", source=TurnSource.TEXT)
    )
    assert "прошлых реплик не было 3 дн" in out


async def test_voice_keeps_both_time_and_preamble():
    out = await _prompt("поставь пайтон", TurnSource.VOICE)
    assert out.startswith("[сейчас ")
    assert VOICE_PREAMBLE in out
    assert out.endswith("поставь Python")


async def test_slash_commands_reach_claude_untouched():
    """Регрессия: штамп времени перед «/compact» ломал саму команду.

    Claude Code разбирает свои команды только в начале сообщения. Стоило
    приписать перед ними что угодно — и /compact переставал сворачивать
    контекст, молча превращаясь в обычную реплику.
    """
    for command in ("/compact", "/context", "/model opus", "  /compact сохрани план"):
        assert await _prompt(command, TurnSource.TEXT) == command


async def test_agent_turns_carry_no_extra_header():
    out = await _prompt("[Это автономный ход…] ЦЕЛЬ #1", TurnSource.AGENT)
    assert out.startswith("[Это автономный ход")


async def test_clear_queue_drops_all_waiting():
    """Кнопка «Отмена очереди» снимает всё ожидающее и не падает на пустой."""
    service = _service()
    for i in range(3):
        service._queue.put_nowait(
            TurnRequest(chat_id=1, text=f"t{i}", source=TurnSource.TEXT)
        )
    assert service.queued == 3
    assert service.clear_queue() == 3
    assert service.queued == 0
    assert service.clear_queue() == 0  # повторная очистка пустой — ноль, без ошибок
