"""Единица работы: репозитории внутри одной транзакции.

Проверяется механика подмены соединения — что все репозитории получают ровно то
же соединение, на котором открыта транзакция. Без этого атомарность была бы
иллюзией: каждый репозиторий брал бы своё соединение из пула.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from app.infrastructure.db.unit_of_work import BoundConnection, UnitOfWork


class FakeConnection:
    def __init__(self, name): self.name = name


class FakeDatabase:
    def __init__(self):
        self.opened = 0
        self.committed = False

    @asynccontextmanager
    async def transaction(self):
        self.opened += 1
        yield FakeConnection("транзакционное")
        self.committed = True

    @asynccontextmanager
    async def connection(self):
        yield FakeConnection("из пула")


async def test_all_repositories_share_one_connection(tmp_path):
    db = FakeDatabase()
    async with UnitOfWork(db, tmp_path / "state.json").begin() as repos:
        used = []
        for repo in (repos.turns, repos.notes, repos.deployments, repos.terms, repos.kv):
            async with repo._db.connection() as conn:
                used.append(conn)
    assert db.opened == 1
    assert len({id(c) for c in used}) == 1, "репозитории разошлись по разным соединениям"
    assert used[0].name == "транзакционное"


async def test_transaction_commits_on_clean_exit(tmp_path):
    db = FakeDatabase()
    async with UnitOfWork(db, tmp_path / "state.json").begin():
        pass
    assert db.committed


async def test_bound_connection_never_opens_a_new_one():
    conn = FakeConnection("единственное")
    bound = BoundConnection(conn)
    async with bound.connection() as first, bound.transaction() as second:
        assert first is conn and second is conn
