"""Отмена распознавания голосового: кнопка «Прервать» отменяет свою задачу.

Проверяем чистую механику реестра задач, без Телеграма: запущенная задача
отменяется по ключу, неизвестная и уже завершённая — нет.
"""
from __future__ import annotations

import asyncio

import pytest

from app.presentation.routers.files_router import _STT_JOBS, _cancel_stt_job


async def test_cancel_running_job():
    async def slow():
        await asyncio.sleep(30)

    task = asyncio.create_task(slow())
    _STT_JOBS[123] = task
    try:
        assert _cancel_stt_job(123) is True
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
    finally:
        _STT_JOBS.pop(123, None)


async def test_cancel_unknown_job_is_false():
    assert _cancel_stt_job(999999) is False


async def test_cancel_finished_job_is_false():
    async def quick():
        return 1

    task = asyncio.create_task(quick())
    await task
    _STT_JOBS[7] = task
    try:
        assert _cancel_stt_job(7) is False
    finally:
        _STT_JOBS.pop(7, None)
