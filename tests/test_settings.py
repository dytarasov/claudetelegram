"""Настройки: разбор списка id — место, где уже ломались."""
from __future__ import annotations

from app.settings import Settings


def _settings(**kw):
    # _env_file=None: тест не должен зависеть от настоящего .env на машине.
    return Settings(_env_file=None, telegram_bot_token="t", database_dsn="d", **kw)


def test_ids_from_comma_string():
    assert _settings(allowed_user_ids="1, 2 3").allowed_user_ids == {1, 2, 3}


def test_ids_from_single_number():
    """pydantic-settings разбирает одиночное значение как JSON и отдаёт int."""
    assert _settings(allowed_user_ids=494317179).allowed_user_ids == {494317179}


def test_problems_lists_missing_pieces():
    problems = Settings(_env_file=None).problems()
    assert any("TELEGRAM_BOT_TOKEN" in p for p in problems)
    assert any("ALLOWED_USER_IDS" in p for p in problems)


def test_no_problems_when_configured():
    assert _settings(allowed_user_ids="1").problems() == []


def test_derived_paths_hang_off_root():
    s = _settings(allowed_user_ids="1")
    assert s.heartbeat_file.parent == s.run_dir
    assert s.upload_dir.parent == s.workspace


def test_paths_follow_root_dir_override(tmp_path):
    """Регрессия: пути считались от константы модуля, и тесты писали в боевой
    каталог, затирая метку текущей выкатки прямо во время preflight."""
    s = Settings(_env_file=None, telegram_bot_token="t", allowed_user_ids="1",
                 database_dsn="d", root_dir=tmp_path)
    assert s.root == tmp_path
    assert s.update_file == tmp_path / "run" / "update.json"
    assert s.heartbeat_file == tmp_path / "run" / "heartbeat.json"
    assert s.guard_script == tmp_path / "scripts" / "guard.sh"
    assert s.state_file == tmp_path / "state.json"
