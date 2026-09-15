"""Конфиг MCP-серверов, которые claude поднимает вместе с сессией.

Файл генерируется на старте, а не лежит в репозитории, по одной причине: в нём
абсолютные пути к venv и к скрипту, а они берутся из настроек. Захардкоженный
путь пережил бы ровно до первого переезда каталога и сломался бы молча — claude
не падает, если MCP-сервер не запустился, он просто работает без инструментов.

Живёт файл в run/ (не под git): это производное состояние, как heartbeat.json.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def write_mcp_config(run_dir: Path, python: Path, src_dir: Path) -> Path | None:
    """Записать run/mcp.json и вернуть путь. None, если сервера нет на месте.

    Проверка существования скрипта здесь не формальность: после отката на
    старую версию модуля памяти может не быть, и тогда честнее вообще не
    передавать --mcp-config, чем каждую сессию тратить время на запуск того,
    чего нет.
    """
    server = src_dir / "app" / "mcp_server.py"
    if not server.exists() or not python.exists():
        log.warning("MCP-сервер памяти не найден (%s) — работаю без него", server)
        return None

    config = {
        "mcpServers": {
            # Имя сервера попадает в имена инструментов: mcp__memory__recall.
            # Поэтому оно короткое — длинное каждый раз занимало бы место в
            # описании доступных инструментов.
            "memory": {
                "command": str(python),
                "args": [str(server)],
                # Пустой env не значит «без переменных»: claude передаёт своё
                # окружение, а настройки сервер и так читает из .env по
                # абсолютному пути от собственного расположения.
                "env": {},
            }
        }
    }
    path = run_dir / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("MCP-конфиг записан: %s", path)
    return path
