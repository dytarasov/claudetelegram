"""Обёртка над systemd — реализация порта ServiceControl.

Здесь же живёт единственный по-настоящему хитрый приём всего проекта:
spawn_detached. Сторож обязан пережить перезапуск бота, а `systemctl restart`
убивает весь cgroup юнита — значит, запускать сторожа из процесса бота обычным
Popen нельзя, он умрёт вместе с ним. systemd-run создаёт отдельный transient-юнит,
то есть отдельный cgroup, и сторож остаётся жив.
"""
from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Sequence

from ...domain.errors import InfrastructureError
from .shell import run

log = logging.getLogger(__name__)


class SystemdClient:
    def restart(self, unit: str) -> None:
        run(["systemctl", "restart", unit], timeout=120)

    def is_active(self, unit: str) -> bool:
        return run(["systemctl", "is-active", unit], check=False) == "active"

    def spawn_detached(self, name: str, argv: Sequence[str]) -> str:
        """Запустить команду вне своего cgroup. Возвращает имя созданного юнита."""
        if not shutil.which("systemd-run"):
            raise InfrastructureError(
                "нет systemd-run: сторожа некуда вынести, выкатка без присмотра запрещена"
            )
        unit = f"{name}-{int(time.time())}"
        run([
            "systemd-run", "--collect", f"--unit={unit}",
            "--description=claude-tg self-update guard", *argv,
        ])
        log.info("сторож запущен отдельным юнитом %s", unit)
        return unit
