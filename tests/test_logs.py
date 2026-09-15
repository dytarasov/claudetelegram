"""Чтение логов: хвост файла, фильтр, выгрузка.

Логи растут до мегабайтов, а нужны почти всегда последние строки — поэтому
читаем с конца кусками. Тут проверяется, что эта оптимизация не теряет строки:
если в первом куске подходящих мало, читатель обязан отступить дальше назад.
"""
from __future__ import annotations

import gzip

import pytest

from app.domain.errors import UserError
from app.infrastructure.logs.reader import FileLogReader
from app.services.logs import LogService
from app.settings import Settings


@pytest.fixture
def logs(tmp_path):
    settings = Settings(_env_file=None, telegram_bot_token="t", allowed_user_ids="1",
                        database_dsn="d", root_dir=tmp_path)
    settings.run_dir.mkdir(parents=True)
    return settings, FileLogReader(settings)


def _write(settings, name, lines):
    (settings.run_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_tail_returns_last_lines_in_order(logs):
    settings, reader = logs
    _write(settings, "events.log", [f"строка {i}" for i in range(100)])
    assert reader.tail("events", 3) == ["строка 97", "строка 98", "строка 99"]


def test_tail_of_missing_stream_is_empty(logs):
    _settings, reader = logs
    assert reader.tail("events", 10) == []
    assert reader.path("events") is None


def test_filter_is_case_insensitive(logs):
    settings, reader = logs
    _write(settings, "events.log", ["ход начат", "ХОД завершён", "выкатка"])
    assert reader.tail("events", 10, "ход") == ["ход начат", "ХОД завершён"]


def test_reads_further_back_when_first_chunk_lacks_matches(logs):
    """Совпадение в самом начале большого файла всё равно должно найтись."""
    settings, reader = logs
    lines = ["иголка в сене"] + [f"пустая строка {i} " + "x" * 200 for i in range(5000)]
    _write(settings, "events.log", lines)
    assert reader.tail("events", 5, "иголка") == ["иголка в сене"]


def test_never_returns_a_truncated_line(logs):
    """Читаем с середины файла, поэтому первая строка куска почти всегда обрезана.

    Отдавать огрызок нельзя: в логе это выглядит как искажённое событие. Строки
    здесь заведомо длиннее читаемого куска, так что случай гарантированно
    воспроизводится.
    """
    settings, reader = logs
    _write(settings, "events.log", [f"{i:04d} " + "д" * 50_000 for i in range(20)])
    got = reader.tail("events", 3)
    assert len(got) == 3
    assert all(len(line) == 50_005 for line in got), "вернулся обрезок строки"


def test_streams_lists_only_existing(logs):
    settings, reader = logs
    _write(settings, "guard.log", ["сторож"])
    assert reader.streams() == ["guard"]


# --------------------------------------------------------------------------- #
#  сервис
# --------------------------------------------------------------------------- #
def test_aliases_resolve_to_streams(logs):
    service = LogService(logs[1])
    assert service.resolve(None) == "events"
    assert service.resolve("события") == "events"
    assert service.resolve("подробно") == "app"
    assert service.resolve("сторож") == "guard"


def test_unknown_alias_is_a_user_error(logs):
    with pytest.raises(UserError):
        LogService(logs[1]).resolve("чепуха")


def test_problems_keeps_only_alarming_lines(logs):
    settings, reader = logs
    _write(settings, "app.log", [
        "2026-09-01 01:00:00 INFO    app: обычная строка",
        "2026-09-01 01:00:01 WARNING app: что-то не так",
        "2026-09-01 01:00:02 ERROR   app: совсем плохо",
    ])
    problems = LogService(reader).problems(10)
    assert len(problems) == 2
    assert all("INFO" not in line for line in problems)


def test_export_copies_small_log_as_is(logs):
    settings, reader = logs
    _write(settings, "events.log", ["строка"])
    path, compressed = LogService(reader).export("events")
    assert not compressed
    assert path.read_text(encoding="utf-8").strip() == "строка"


def test_export_compresses_big_log(logs):
    settings, reader = logs
    _write(settings, "app.log", [f"строка {i} " + "д" * 100 for i in range(30000)])
    path, compressed = LogService(reader).export("app")
    assert compressed and path.suffix == ".gz"
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        assert "строка 0" in fh.readline()


def test_export_of_empty_stream_is_a_user_error(logs):
    with pytest.raises(UserError):
        LogService(logs[1]).export("events")
