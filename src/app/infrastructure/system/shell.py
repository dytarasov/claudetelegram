"""Запуск внешних команд одним способом на весь проект.

Смысл файла — чтобы нигде больше не было subprocess.run со своим набором
флагов. Здесь единая политика: не поднимать shell, всегда собирать текст,
кидать InfrastructureError с внятным текстом, если команда упала.
"""
from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence

from ...domain.errors import InfrastructureError

log = logging.getLogger(__name__)


def run(argv: Sequence[str], *, cwd: str | None = None, check: bool = True,
        timeout: float = 60.0) -> str:
    """Выполнить команду и вернуть stdout без завершающего перевода строки.

    rstrip только по '\\n': у `git status --porcelain` первые два символа —
    статус, и общий strip() съедал бы ведущий пробел, сдвигая разбор имени
    файла. На этом уже обжигались — не менять.
    """
    try:
        res = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InfrastructureError(f"{argv[0]}: {exc}") from exc
    if check and res.returncode != 0:
        detail = (res.stderr.strip() or res.stdout.strip() or f"код {res.returncode}")
        raise InfrastructureError(f"{' '.join(argv[:3])}: {detail}")
    return res.stdout.rstrip("\n")
