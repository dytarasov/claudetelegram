"""Гибридный поиск по памяти и нарезка на куски.

Проверяется не «работает ли pgvector» — это забота базы, — а решения сервиса:
что происходит без эмбеддера, как сливаются два списка результатов, что поиск
показывает найденное место, а не только запись, и что индексация не
пересчитывает уже посчитанное.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.domain.models import Chunk, Note, Turn
from app.infrastructure.repositories.base import or_terms, vector_literal
from app.services.journal import JournalService

NOW = datetime(2026, 9, 1, 12, 0)


def _turn(turn_id: int, prompt: str, answer: str = "ответ") -> Turn:
    return Turn(id=turn_id, chat_id=1, user_id=1, prompt=prompt, answer=answer,
                created_at=NOW)


def _chunk(chunk_id: int, source_id: int, text: str, role: str = "assistant") -> Chunk:
    return Chunk(id=chunk_id, source_kind="turn", source_id=source_id, ord=0,
                 role=role, text=text, created_at=NOW)


class FakeTurns:
    def __init__(self):
        self.items = {
            1: _turn(1, "что со сторожем", "сторож откатывает версию, если пульс молчит"),
            2: _turn(2, "как идёт выкатка", "выкатка проходит преflight и уходит под присмотр"),
            3: _turn(3, "наблюдение за версией", "версия досиживает выдержку"),
        }

    async def by_ids(self, ids): return {i: self.items[i] for i in ids if i in self.items}
    async def recent(self, limit=10): return list(self.items.values())[:limit]
    async def stats(self): return {"turns": len(self.items)}


class FakeChunks:
    """Полнотекст и вектор отдают РАЗНЫЕ куски — так видно работу фузии."""

    def __init__(self):
        self.fts = [(_chunk(11, 1, "сторож откатывает версию", "assistant"), 0.9),
                    (_chunk(12, 2, "выкатка под присмотром"), 0.5)]
        self.semantic = [(_chunk(13, 3, "версия досиживает выдержку"), 0.8),
                         (_chunk(11, 1, "сторож откатывает версию"), 0.7)]
        self.pending = [(10, "текст десять"), (11, "текст одиннадцать")]
        self.saved: dict[int, tuple[list[float], str]] = {}
        self.rebuilt: list[tuple[str, int, int]] = []
        self.unchunked = {"turn": [], "note": []}

    async def search_text(self, query, limit=20): return self.fts[:limit]
    async def search_semantic(self, vector, model, limit=20): return self.semantic[:limit]
    async def pending_embeddings(self, model, limit=100): return self.pending[:limit]

    async def set_embedding(self, chunk_id, vector, model):
        self.saved[chunk_id] = (vector, model)
        self.pending = [(i, t) for i, t in self.pending if i != chunk_id]

    async def sources_without_chunks(self, kind, limit=50):
        ids, self.unchunked[kind] = self.unchunked[kind][:limit], []
        return ids

    async def rebuild(self, kind, source_id, created_at, pieces):
        self.rebuilt.append((kind, source_id, len(pieces)))
        return len(pieces)

    async def stats(self, model):
        return {"total": 3, "indexed": len(self.saved), "sources": 3}


class FakeNotes:
    def __init__(self): self.items: list[Note] = []
    async def add(self, note): note.id = len(self.items) + 1; self.items.append(note); return note
    async def search(self, query, limit=10): return self.items[:limit]
    async def recent(self, limit=10): return self.items[:limit]
    async def delete(self, note_id): return False


class FakeEmbedder:
    def __init__(self, works=True, dim=4):
        self.works, self.dim, self.calls = works, dim, 0
    def available(self): return self.works
    def dimensions(self): return self.dim
    def model_name(self): return "fake-embed-1"
    async def embed(self, texts):
        self.calls += 1
        if not self.works:
            raise RuntimeError("провайдер лёг")
        return [[0.1] * self.dim for _ in texts]


def _service(embedder=None, chunks=None):
    return JournalService(FakeTurns(), FakeNotes(), chunks or FakeChunks(),
                          embedder or FakeEmbedder())


# --- поиск ------------------------------------------------------------------ #
async def test_without_embedder_search_is_full_text_only():
    service = _service(FakeEmbedder(works=False))
    found = await service.recall("сторож", 5)
    assert [e.id for e in found] == [1, 2]
    assert service.semantic_available is False


async def test_hybrid_merges_both_sources():
    found = await _service().recall("сторож", 5)
    assert {e.id for e in found} == {1, 2, 3}, "куски из обоих списков должны попасть в ответ"


async def test_record_found_by_both_methods_wins():
    """Ход 1 нашли оба метода — он и должен быть первым."""
    assert (await _service().recall("сторож", 5))[0].id == 1


async def test_search_shows_the_matching_passage_not_just_the_record():
    """Ради этого и затевалась нарезка: видно место в разговоре, а не факт разговора."""
    found = await _service().recall("сторож", 5)
    top = found[0]
    assert top.passage == "сторож откатывает версию"
    assert top.said_by == "assistant"


async def test_several_chunks_of_one_turn_do_not_multiply_it():
    """Ход, где тема всплыла дважды, встречается в выдаче один раз — но выше."""
    chunks = FakeChunks()
    chunks.fts = [(_chunk(11, 1, "сторож раз"), 0.9), (_chunk(14, 1, "сторож два"), 0.8)]
    chunks.semantic = []
    found = await _service(chunks=chunks).recall("сторож", 5)
    assert [e.id for e in found] == [1]


async def test_broken_embedder_degrades_to_full_text():
    """Чужой API упал — поиск обязан продолжить работать, а не развалиться."""
    class Broken(FakeEmbedder):
        async def embed(self, texts): raise RuntimeError("500")

    found = await _service(Broken()).recall("сторож", 5)
    assert [e.id for e in found] == [1, 2]


async def test_empty_query_returns_recent_without_calling_provider():
    embedder = FakeEmbedder()
    found = await _service(embedder).recall("", 5)
    assert len(found) == 3
    assert embedder.calls == 0, "на пустой запрос ходить в API незачем"


# --- индексация -------------------------------------------------------------- #
async def test_indexing_stores_vectors_and_stops_when_done():
    chunks, embedder = FakeChunks(), FakeEmbedder()
    service = _service(embedder, chunks)
    assert await service.index_pending(10) == 2
    assert set(chunks.saved) == {10, 11}
    assert chunks.saved[10][1] == "fake-embed-1"
    assert await service.index_pending(10) == 0, "повторный проход не должен ничего делать"


async def test_new_turns_get_chunked_before_vectors():
    """Нарезка первее векторов: иначе свежий ход невидим даже для полнотекста."""
    chunks = FakeChunks()
    chunks.unchunked["turn"] = [1, 2]
    made = await _service(chunks=chunks).index_pending(10)
    assert [kind for kind, _id, _n in chunks.rebuilt] == ["turn", "turn"]
    assert made > 0


async def test_indexing_is_a_noop_without_embedder():
    """Без эмбеддера нарезка всё равно идёт — полнотекст по кускам работает."""
    chunks = FakeChunks()
    service = _service(FakeEmbedder(works=False), chunks)
    assert await service.index_pending(10) == 0
    assert chunks.saved == {}


async def test_memory_stats_reports_coverage():
    service = _service()
    await service.index_pending(10)
    stats = await service.memory_stats()
    assert stats == {"available": True, "model": "fake-embed-1", "dim": 4,
                     "turns": 3, "total": 3, "indexed": 2, "sources": 3}


# --- мелочи, на которых всё держится ----------------------------------------- #
@pytest.mark.parametrize("values,expected", [
    ([0.1, 0.2], "[0.1,0.2]"),
    ([1.0, -0.5], "[1,-0.5]"),
    ([], "[]"),
])
def test_vector_literal_format(values, expected):
    """pgvector принимает вектор текстом — формат должен быть ровно таким."""
    assert vector_literal(values) == expected


def test_or_terms_keeps_single_word_intact():
    assert or_terms("сторож") == "сторож"
