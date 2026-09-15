"""Память как рабочий инструмент, а не как хранилище.

Этот сервис существует, потому что у памяти обнаружилась дыра в применении:
слои были построены (журнал ходов, векторный индекс, гибридный поиск), но
дотянуться до них в разговоре было нечем — поиск висел на команде /recall,
которую надо набрать руками. Память, к которой не обращаются, не память.

MemoryService — единая точка входа для всех слоёв сразу:

    профиль   закреплённые факты и открытые обязательства. Читается перед
              работой целиком, без поиска: это то, что нельзя не знать.
    факты     устойчивые утверждения с ключом и сроком действия.
    события   то, что случилось или предстоит, с датой известной точности.
    журнал    сырые ходы разговора; тут и работает семантика.

Форматы возврата — простые словари, а не доменные модели. Наверху сидит не
питон-код, а языковая модель через MCP, и ей нужен текст с явными полями, а не
объекты. Заодно это отсекает соблазн протащить asyncpg-строку до самого верха.

Осознанное решение про запись: писать в память может только тот, кто уверен.
Поэтому у каждого факта есть source (сказано человеком или выведено мной) и
confidence, а не удаление, а закрытие датой — см. миграцию 0007.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..domain.models import Event, Fact
from ..domain.ports import (EventRepository, FactRepository, ReminderRepository)
from ..settings import Settings
from .journal import JournalService

log = logging.getLogger(__name__)

# Сколько закреплённых фактов имеет смысл отдавать. Профиль ценен, пока
# помещается в голову целиком; сотня «важных» фактов — это уже не профиль.
PROFILE_LIMIT = 60


class MemoryService:
    def __init__(self, facts: FactRepository, events: EventRepository,
                 reminders: ReminderRepository, journal: JournalService,
                 settings: Settings) -> None:
        self._facts = facts
        self._events = events
        self._reminders = reminders
        self._journal = journal
        self._settings = settings

    # ---- чтение ----------------------------------------------------------- #
    async def profile(self, subject: str | None = None) -> dict:
        """Что надо знать всегда: закреплённые факты и незакрытые обязательства.

        Сюда же кладётся текущее время. Кажется мелочью, но без него любое
        рассуждение о сроках («послезавтра», «уже просрочено») строится на
        догадке о том, какой сегодня день.
        """
        now = datetime.now(self._settings.tz)
        facts = await self._facts.pinned(PROFILE_LIMIT)
        if subject:
            facts = [f for f in facts if f.subject == subject]

        open_items: list[dict] = []
        chat_id = self._owner_chat_id()
        if chat_id is not None:
            for item in await self._reminders.open_items(chat_id, 20):
                open_items.append({
                    "id": item.id,
                    "text": item.text,
                    "due": item.due_at.astimezone(self._settings.tz).strftime("%d.%m %H:%M"),
                    "overdue": item.overdue_for > 0,
                    "ignored_times": item.fired_count,
                })

        return {
            "now": now.strftime("%A, %d.%m.%Y %H:%M %Z"),
            "facts": [self._fact_dict(f) for f in facts],
            "open_commitments": open_items,
            "hint": ("Профиль — только закреплённое. Остальное ищется через recall; "
                     "события с датами — через timeline."),
        }

    async def recall(self, query: str, kind: str = "all", limit: int = 8) -> dict:
        """Поиск сразу по всем слоям. kind сужает область, если она известна.

        Возвращаются все слои разом не из щедрости: на вопрос «когда я обещал
        отдать деньги» ответ лежит в событиях, а обоснование — в разговоре, и
        заранее угадать нужный слой нельзя.
        """
        result: dict = {"query": query}
        if kind in ("all", "facts"):
            result["facts"] = [self._fact_dict(f)
                               for f in await self._facts.search(query, limit)]
        if kind in ("all", "events"):
            result["events"] = [self._event_dict(e)
                                for e in await self._events.search(query, limit)]
        if kind in ("all", "turns"):
            entries = await self._journal.recall(query, limit)
            result["turns"] = [{
                "id": e.id,
                "when": e.when.astimezone(self._settings.tz).strftime("%d.%m.%Y %H:%M"),
                "asked": self._head(e.prompt),
                # Совпавший кусок — то, ради чего затевалась нарезка: видно
                # место в разговоре, а не только факт «был такой разговор».
                "match": e.passage or e.answer_head,
                "said_by": e.said_by or "assistant",
            } for e in entries]
            result["semantic"] = self._journal.semantic_available
        if kind in ("all", "notes"):
            result["notes"] = [{"id": n.id, "text": n.text, "tags": n.tags}
                               for n in await self._journal.find_notes(query, limit)]
        return result

    async def timeline(self, since: datetime, until: datetime, limit: int = 50) -> dict:
        """События в окне времени. Именно так и спрашивают про прошлое."""
        found = await self._events.between(since, until, limit)
        return {
            "from": since.astimezone(self._settings.tz).strftime("%d.%m.%Y"),
            "to": until.astimezone(self._settings.tz).strftime("%d.%m.%Y"),
            "events": [self._event_dict(e) for e in found],
        }

    async def history(self, subject: str, key: str) -> dict:
        """Как менялся один факт. Отвечает на «почему ты так решил»."""
        versions = await self._facts.history(subject, key)
        return {"subject": subject, "key": key,
                "versions": [self._fact_dict(f) for f in versions]}

    async def stats(self) -> dict:
        semantic = await self._journal.memory_stats()
        turns = await self._journal.stats()
        return {
            "facts_active": len(await self._facts.active(limit=1000)),
            "pinned": len(await self._facts.pinned(1000)),
            "events": await self._events.count(),
            "turns": turns,
            "semantic": semantic,
        }

    # ---- запись ------------------------------------------------------------ #
    async def remember_fact(self, value: str, *, subject: str = "user",
                            key: str | None = None, kind: str = "fact",
                            pinned: bool = False, confidence: float = 1.0,
                            tags: list[str] | None = None,
                            source: str = "assistant") -> Fact:
        """Запомнить утверждение. С тем же subject+key прежнее уходит в историю."""
        fact = Fact(value=value.strip(), subject=subject.strip() or "user",
                    key=(key or "").strip() or None, kind=kind, pinned=pinned,
                    confidence=max(0.0, min(1.0, confidence)), tags=tags or [],
                    source=source)
        saved = await self._facts.add(fact)
        log.info("запомнил факт #%s %s/%s", saved.id, saved.subject, saved.key or "-")
        return saved

    async def remember_event(self, title: str, happened_at: datetime, *,
                             details: str | None = None, date_precision: str = "minute",
                             kind: str = "event", people: list[str] | None = None,
                             tags: list[str] | None = None,
                             source: str = "assistant") -> Event:
        event = Event(title=title.strip(), happened_at=happened_at, details=details,
                      date_precision=date_precision, kind=kind, people=people or [],
                      tags=tags or [], source=source)
        saved = await self._events.add(event)
        log.info("запомнил событие #%s на %s", saved.id, saved.when_text(self._settings.tz))
        return saved

    async def add_note(self, text: str, tags: list[str] | None = None):
        return await self._journal.add_note(text, tags)

    async def retire_fact(self, fact_id: int) -> bool:
        return await self._facts.retire(fact_id)

    async def forget_event(self, event_id: int) -> bool:
        return await self._events.delete(event_id)

    # ---- внутреннее -------------------------------------------------------- #
    @staticmethod
    def _head(text: str, limit: int = 200) -> str:
        text = " ".join((text or "").split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def _owner_chat_id(self) -> int | None:
        """В личной переписке chat_id совпадает с user_id.

        Пользователь тут ровно один, поэтому обязательства достаются без лишней
        параметризации. Если разрешённых окажется несколько, честнее не гадать.
        """
        ids = self._settings.allowed_user_ids
        return next(iter(ids)) if len(ids) == 1 else None

    def _fact_dict(self, fact: Fact) -> dict:
        data = {
            "id": fact.id,
            "subject": fact.subject,
            "key": fact.key,
            "value": fact.value,
            "kind": fact.kind,
            "pinned": fact.pinned,
            "source": fact.source,
        }
        if fact.confidence < 1.0:
            data["confidence"] = round(fact.confidence, 2)
        if not fact.active:
            data["retired_at"] = fact.valid_until.strftime("%d.%m.%Y")
            data["superseded_by"] = fact.superseded_by
        return data

    def _event_dict(self, event: Event) -> dict:
        return {
            "id": event.id,
            "when": event.when_text(self._settings.tz),
            "title": event.title,
            "details": event.details,
            "kind": event.kind,
            "people": event.people,
            "future": event.happened_at > datetime.now(self._settings.tz),
        }


def window(days_back: int = 30, days_ahead: int = 0,
           tz=None) -> tuple[datetime, datetime]:
    """Окно вокруг сегодня — самый частый запрос к событиям."""
    now = datetime.now(tz)
    return now - timedelta(days=days_back), now + timedelta(days=days_ahead + 1)
