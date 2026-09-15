"""Сборка роутеров в одно дерево.

Порядок включения важен: команды идут первыми, а text_router ловит вообще любой
текст и потому обязан быть последним — иначе он перехватил бы всё, включая
/status и /rollback.
"""
from __future__ import annotations

from aiogram import Router

from . import (files_router, goals_router, help_router, input_router,
               journal_router, logs_router, reminders_router, selfupdate_router,
               session_router, text_router, ui_router)


def build_root_router() -> Router:
    root = Router(name="root")
    root.include_routers(
        help_router.router,
        session_router.router,
        selfupdate_router.router,
        journal_router.router,
        goals_router.router,
        logs_router.router,
        reminders_router.router,
        ui_router.router,
        files_router.router,
        input_router.router,  # ответ на вопрос кнопки, иначе пропускает дальше
        text_router.router,   # всегда последний: ловит любой текст
    )
    return root
