"""Живой словарь терминов распознавания.

Идея простая: словарь не должен быть окаменелостью в коде. Затравка лежит в
domain/terms.py, всё остальное — в таблице stt_terms, и пополняется прямо по
ходу разговора. Услышал в расшифровке новое кривое слово — добавил, и оно
чинится начиная со следующего голосового: ни правки кода, ни выкатки, ни
перезапуска.

Скомпилированный индекс кэшируется (сборка регэкспа на сотню вариантов дешёвая,
но делать её на каждое голосовое незачем) и сбрасывается при любом изменении.

Если база недоступна, сервис молча работает на одной затравке: потерять
исправление терминов не смертельно, а вот падать из-за этого — глупо.
"""
from __future__ import annotations

import asyncio
import logging

from ..domain.models import SttTerm
from ..domain.ports import TermRepository
from ..domain.terms import SEED_TERMS, Replacement, TermIndex

from . import events

log = logging.getLogger(__name__)


class TermsService:
    def __init__(self, repository: TermRepository) -> None:
        self._repo = repository
        self._index: TermIndex | None = None
        self._lock = asyncio.Lock()

    # ---- чтение ---------------------------------------------------------- #
    async def mapping(self) -> dict[str, list[str]]:
        """Затравка плюс всё, что добавили руками."""
        merged: dict[str, list[str]] = {c: list(v) for c, v in SEED_TERMS.items()}
        try:
            for term in await self._repo.all():
                merged.setdefault(term.canonical, [])
                if term.variant not in merged[term.canonical]:
                    merged[term.canonical].append(term.variant)
        except Exception:  # noqa: BLE001
            log.warning("словарь из базы недоступен, работаю на затравке", exc_info=True)
        return merged

    async def index(self) -> TermIndex:
        async with self._lock:
            if self._index is None:
                self._index = TermIndex(await self.mapping())
            return self._index

    async def normalize(self, text: str) -> tuple[str, list[Replacement]]:
        fixed, applied = (await self.index()).apply(text)
        if applied:
            log.info("поправил термины: %s", ", ".join(str(r) for r in applied))
        return fixed, applied

    # ---- изменение -------------------------------------------------------- #
    async def learn(self, canonical: str, variants: list[str],
                    source: str = "assistant") -> list[SttTerm]:
        """Запомнить, что такие-то услышанные варианты означают такой-то термин."""
        saved = []
        for variant in variants:
            variant = variant.strip()
            if variant:
                saved.append(await self._repo.add(
                    SttTerm(canonical=canonical.strip(), variant=variant, source=source)
                ))
        if saved:
            await self._reset()
            events.term_learned(canonical, [t.variant for t in saved])
        return saved

    async def forget(self, variant: str) -> bool:
        """Убрать вариант. Из затравки убрать нельзя — она в коде."""
        removed = await self._repo.remove(variant)
        if removed:
            await self._reset()
        return removed

    async def _reset(self) -> None:
        async with self._lock:
            self._index = None
