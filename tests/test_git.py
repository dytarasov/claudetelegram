"""GitClient на настоящем временном репозитории — без моков.

Главный смысл — регрессия на разбор `git status --porcelain`: первые два
символа строки значащие, и общий strip() съедал ведущий пробел у первой строки,
из-за чего имя файла теряло первый символ (».gitignore» → «gitignore»).
"""
from __future__ import annotations

import subprocess

import pytest

from app.infrastructure.system.git import GitClient


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                       capture_output=True, text=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    (tmp_path / ".gitignore").write_text("venv/\n")
    (tmp_path / "file.txt").write_text("раз\n")
    git("add", "-A")
    git("commit", "-qm", "первый")
    return tmp_path


def test_head_and_subject(repo):
    client = GitClient(repo)
    assert len(client.head()) == 40
    assert client.subject("HEAD") == "первый"


def test_dirty_keeps_leading_dot_in_filename(repo):
    """Файл, начинающийся с точки, и статус с ведущим пробелом — та самая ловушка."""
    (repo / ".gitignore").write_text("venv/\nrun/\n")
    dirty = GitClient(repo).dirty()
    assert (".gitignore" in [path for _, path in dirty])
    assert all(not path.startswith("gitignore") for _, path in dirty)


def test_dirty_reports_untracked(repo):
    (repo / "new.py").write_text("x = 1\n")
    statuses = dict((path, status) for status, path in GitClient(repo).dirty())
    assert statuses["new.py"] == "??"


def test_commit_and_tags(repo):
    client = GitClient(repo)
    (repo / "file.txt").write_text("два\n")
    sha = client.commit("второй", ["."])
    assert client.subject(sha) == "второй"

    client.tag("stable", sha)
    assert client.tag_sha("stable") == sha
    assert client.tag_sha("нет-такой-метки") is None


def test_resolve_understands_steps_back(repo):
    client = GitClient(repo)
    (repo / "file.txt").write_text("два\n")
    first = client.head()
    second = client.commit("второй", ["."])
    assert client.resolve("1") == first
    assert client.resolve("HEAD") == second


def test_reset_hard_returns_previous_content(repo):
    client = GitClient(repo)
    first = client.head()
    (repo / "file.txt").write_text("два\n")
    client.commit("второй", ["."])
    client.reset_hard(first)
    assert (repo / "file.txt").read_text() == "раз\n"
