"""Моя память: поиск по прошлым ходам, заметки и векторный индекс.

Зачем это вообще. Контекст сессии конечен и рано или поздно сворачивается
(/compact) или начинается заново. Всё, что было сказано раньше, для меня
исчезает. Журнал — единственный способ ответить на «а когда я трогал сторожа и
чем это кончилось» через неделю, когда самого разговора в контексте уже нет.

Разделение источников:
  • turns — сырые ходы, пишутся автоматически после каждого ответа;
  • notes — осознанные выводы, добавляются командой /note.
Первое отвечает на «что было», второе — на «что мы из этого поняли».

Единица поиска — не запись, а кусок (см. services/chunking.py и миграцию 0008).
Ход целиком слишком крупен: у длинного ответа получается один усреднённый
вектор, не похожий ни на одну из его мыслей, а хвост вообще не попадал в индекс
из-за обрезки. Поэтому ищем по кускам, а показываем найденную запись целиком
плюс тот самый кусок, который совпал.

Поиск гибридный, и это не украшательство. Полнотекст находит точные слова
(«pgvector», «сторож»), но не понимает синонимов; вектор понимает смысл, но
плохо ловит редкие термины и имена. Каждый по отдельности регулярно
промахивается там, где второй сработал бы. Результаты сливаются ранговой фузией
(RRF): важно не абсолютное значение похожести — они у двух методов
несопоставимы, — а место записи в каждом списке.

Если эмбеддера нет (ключ не задан), всё честно вырождается в полнотекст.
"""
from __future__ import annotations

import logging

from ..domain.dto import JournalEntry
from ..domain.models import Note
from ..domain.ports import ChunkRepository, Embedder, NoteRepository, TurnRepository
from .chunking import chunk_turn, split

log = logging.getLogger(__name__)

# Константа ранговой фузии. 60 — общепринятое значение: достаточно большое,
# чтобы верхние места не подавляли всё остальное, достаточно малое, чтобы
# порядок вообще имел значение.
RRF_K = 60

# Сколько кусков тянуть из каждого поиска до слияния. Больше, чем нужно на
# выходе: несколько кусков часто оказываются из одного хода, и после схлопывания
# по записям выдача заметно короче исходной.
FETCH_FACTOR = 6


class JournalService:
    def __init__(self, turns: TurnRepository, notes: NoteRepository,
                 chunks: ChunkRepository, embedder: Embedder) -> None:
        self._turns = turns
        self._notes = notes
        self._chunks = chunks
        self._embedder = embedder

    @property
    def semantic_available(self) -> bool:
        return self._embedder.available()

    # ---- поиск ------------------------------------------------------------ #
    async def recall(self, query: str, limit: int = 8) -> list[JournalEntry]:
        """Найти ходы по запросу. Пустой запрос — просто последние."""
        if not query.strip():
            return [self._entry(turn, 0.0) for turn in await self._turns.recent(limit)]

        depth = limit * FETCH_FACTOR
        ranked: dict[int, float] = {}      # id хода -> суммарный ранг
        best_chunk: dict[int, tuple] = {}  # id хода -> лучший совпавший кусок

        for found in (await self._chunks.search_text(query, depth),
                      await self._semantic(query, depth)):
            for place, (chunk, score) in enumerate(found):
                if chunk.source_kind != "turn":
                    continue
                # Ход получает ранг за каждый свой совпавший кусок: запись, где
                # тема всплывает трижды, релевантнее той, где она упомянута раз.
                ranked[chunk.source_id] = ranked.get(chunk.source_id, 0.0) + 1 / (RRF_K + place)
                if place < best_chunk.get(chunk.source_id, (10**9, None))[0]:
                    best_chunk[chunk.source_id] = (place, chunk)

        best = sorted(ranked.items(), key=lambda item: -item[1])[:limit]
        turns = await self._turns.by_ids([turn_id for turn_id, _ in best])
        out: list[JournalEntry] = []
        for turn_id, score in best:
            turn = turns.get(turn_id)
            if turn is None:      # ход удалён между поиском и выборкой
                continue
            _place, chunk = best_chunk[turn_id]
            out.append(self._entry(turn, score, chunk))
        return out

    async def _semantic(self, query: str, limit: int) -> list[tuple]:
        """Векторный поиск. Молча пустой, если эмбеддер недоступен или сломался."""
        if not self._embedder.available():
            return []
        try:
            vector = (await self._embedder.embed([query]))[0]
            return await self._chunks.search_semantic(vector, self._embedder.model_name(), limit)
        except Exception:  # noqa: BLE001 — поиск не должен падать из-за чужого API
            log.warning("семантический поиск не удался, остаётся полнотекстовый",
                        exc_info=True)
            return []

    # ---- заметки ----------------------------------------------------------- #
    async def add_note(self, text: str, tags: list[str] | None = None,
                       source: str = "assistant") -> Note:
        return await self._notes.add(Note(text=text.strip(), tags=tags or [], source=source))

    async def find_notes(self, query: str, limit: int = 10) -> list[Note]:
        return await self._notes.search(query, limit)

    async def drop_note(self, note_id: int) -> bool:
        return await self._notes.delete(note_id)

    async def stats(self) -> dict:
        return await self._turns.stats()

    # ---- индексация -------------------------------------------------------- #
    async def index_pending(self, limit: int = 100) -> int:
        """Догнать индекс: нарезать новые записи и посчитать векторы кускам.

        Зовётся фоном по расписанию, поэтому обязана быть дешёвой при пустой
        работе: пара запросов, которые почти всегда возвращают пусто.

        Порядок важен. Сначала нарезка — иначе свежий ход не попадёт даже в
        полнотекстовый поиск и будет невидим до следующего круга. Векторы
        считаются после и могут отстать на цикл: это ухудшает поиск, но не
        отменяет его.
        """
        made = await self.rebuild_chunks(limit)
        if not self._embedder.available():
            return made
        model = self._embedder.model_name()
        pending = await self._chunks.pending_embeddings(model, limit)
        if pending:
            vectors = await self._embedder.embed([text for _id, text in pending])
            for (chunk_id, _text), vector in zip(pending, vectors):
                await self._chunks.set_embedding(chunk_id, vector, model)
            log.info("посчитал векторы для %d кусков (%s)", len(pending), model)
        return made + len(pending)

    async def rebuild_chunks(self, limit: int = 100) -> int:
        """Нарезать записи, у которых кусков ещё нет. Возвращает число кусков."""
        made = 0
        turn_ids = await self._chunks.sources_without_chunks("turn", limit)
        if turn_ids:
            turns = await self._turns.by_ids(turn_ids)
            for turn_id in turn_ids:
                turn = turns.get(turn_id)
                if turn is None:
                    continue
                pieces = chunk_turn(turn.prompt, turn.answer)
                made += await self._chunks.rebuild("turn", turn_id, turn.created_at, pieces)

        note_ids = await self._chunks.sources_without_chunks("note", limit)
        if note_ids:
            # Заметок мало, отдельного by_ids для них заводить не стоит — но и
            # звать recent внутри цикла нельзя: это запрос на каждую заметку.
            known = {n.id: n for n in await self._notes.recent(500)}
            for note_id in note_ids:
                note = known.get(note_id)
                if note is None:
                    continue
                pieces = [("assistant", piece) for piece in split(note.text)]
                made += await self._chunks.rebuild("note", note_id, note.created_at, pieces)

        if made:
            log.info("нарезал %d кусков", made)
        return made

    async def memory_stats(self) -> dict:
        """Что с индексом: сколько кусков и сколько из них покрыто векторами."""
        turn_stats = await self._turns.stats()
        if not self._embedder.available():
            return {"available": False, "model": "—", "dim": 0,
                    "total": 0, "indexed": 0, "turns": turn_stats.get("turns", 0)}
        chunks = await self._chunks.stats(self._embedder.model_name())
        return {
            "available": True,
            "model": self._embedder.model_name(),
            "dim": self._embedder.dimensions(),
            "turns": turn_stats.get("turns", 0),
            **chunks,
        }

    # ---- внутреннее --------------------------------------------------------- #
    def _entry(self, turn, score: float, chunk=None) -> JournalEntry:
        return JournalEntry(
            id=turn.id or 0, when=turn.created_at, prompt=turn.prompt,
            answer_head=self._head(turn.answer), status=str(turn.status), rank=score,
            passage=self._head(chunk.text, 400) if chunk else "",
            said_by=chunk.role if chunk else "",
        )

    @staticmethod
    def _head(text: str, limit: int = 200) -> str:
        text = " ".join((text or "").split())
        return text if len(text) <= limit else text[: limit - 1] + "…"
