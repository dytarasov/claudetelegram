"""Обёртка над git — реализация порта VersionControl.

Самодопил опирается на git как на хранилище версий: коммит = версия, метка
stable = «сюда возвращаться», метка bad/<дата> = «здесь сломалось, но не
потеряно». Никаких веток: линейная история плюс метки читаются глазами и не
требуют разбираться в мердж-графе в час ночи, когда бот лежит.
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from ...domain.errors import InfrastructureError
from .shell import run


class GitClient:
    """Все операции — в одном репозитории, заданном при создании."""

    def __init__(self, root: Path) -> None:
        self._root = str(root)

    def _git(self, *args: str, check: bool = True) -> str:
        return run(["git", "-C", self._root, *args], check=check)

    def head(self) -> str:
        return self._git("rev-parse", "HEAD")

    def short(self, ref: str) -> str:
        try:
            return self._git("rev-parse", "--short", ref)
        except InfrastructureError:
            return (ref or "")[:8]

    def subject(self, ref: str) -> str:
        try:
            return self._git("log", "-1", "--format=%s", ref)
        except InfrastructureError:
            return "?"

    def resolve(self, ref: str) -> str:
        """Понимает stable, sha, HEAD~2 и голое число — «на N шагов назад»."""
        ref = (ref or "").strip() or "HEAD"
        if ref.isdigit():
            ref = f"HEAD~{ref}"
        return self._git("rev-parse", f"{ref}^{{commit}}")

    def dirty(self) -> list[tuple[str, str]]:
        """[(статус, путь)]. Разбор по позициям: первые два символа — статус."""
        out = self._git("status", "--porcelain")
        result = []
        for line in out.splitlines():
            if len(line) > 3:
                result.append((line[:2].strip(), line[3:]))
        return result

    def commit(self, message: str, paths: Sequence[str]) -> str:
        """Закоммитить перечисленные пути. Возвращает sha нового коммита.

        Автор проставляется явно: у сервиса нет глобального git-конфига, а без
        имени и почты git откажется коммитить. По умолчанию — нейтральная
        подпись бота; кто хочет свою, задаёт стандартные GIT_AUTHOR_NAME и
        GIT_AUTHOR_EMAIL в окружении службы (Environment= в systemd-юните).
        """
        name = os.environ.get("GIT_AUTHOR_NAME", "claude-selfupdate")
        email = os.environ.get("GIT_AUTHOR_EMAIL", "claude-selfupdate@localhost")
        self._git("add", "-A", "--", *paths)
        self._git(
            "-c", f"user.name={name}",
            "-c", f"user.email={email}",
            "commit", "-m", message,
        )
        return self.head()

    def tag(self, name: str, ref: str) -> None:
        self._git("tag", "-f", name, ref)

    def tag_sha(self, name: str) -> str | None:
        try:
            return self._git("rev-parse", f"{name}^{{commit}}")
        except InfrastructureError:
            return None

    def log(self, limit: int) -> list[dict[str, str]]:
        out = self._git("log", f"-{limit}", "--format=%H%x00%h%x00%s%x00%cr")
        items = []
        for line in out.splitlines():
            sha, short, subject, when = line.split("\0")
            items.append({"sha": sha, "short": short, "subject": subject, "when": when})
        return items

    def reset_hard(self, ref: str) -> None:
        self._git("reset", "--hard", ref)
