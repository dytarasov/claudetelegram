"""Склейка входящих: куски разрезанного сообщения собираются в один ход.

Без Телеграма: кормим модуль поддельными сообщениями и проверяем, что за паузу
они склеиваются в одну реплику, одиночное проходит как есть, а разрыв паузой
разводит на два хода.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.presentation.routers.text_router import feed


class FakeConv:
    def __init__(self):
        self.busy = False
        self.enqueued = []

    async def enqueue(self, request):
        self.enqueued.append(request)
        return len(self.enqueued)


class FakeSender:
    async def send(self, *a, **kw):
        pass


def _msg(chat, text, mid=1):
    return SimpleNamespace(chat=SimpleNamespace(id=chat),
                           from_user=SimpleNamespace(id=42),
                           message_id=mid, text=text)


async def test_two_pieces_coalesce_into_one_turn():
    conv, sender = FakeConv(), FakeSender()
    await feed(_msg(101, "Привет,", 1), conv, sender, 0.05)
    await asyncio.sleep(0.02)
    await feed(_msg(101, "как дела?", 2), conv, sender, 0.05)
    await asyncio.sleep(0.15)
    assert len(conv.enqueued) == 1
    assert conv.enqueued[0].text == "Привет,\nкак дела?"
    assert conv.enqueued[0].reply_to == 1   # ответ на первый кусок


async def test_single_message_still_goes():
    conv, sender = FakeConv(), FakeSender()
    await feed(_msg(102, "один", 5), conv, sender, 0.05)
    await asyncio.sleep(0.12)
    assert [r.text for r in conv.enqueued] == ["один"]


async def test_gap_separates_into_two_turns():
    conv, sender = FakeConv(), FakeSender()
    await feed(_msg(103, "первый", 1), conv, sender, 0.05)
    await asyncio.sleep(0.12)   # пауза прошла — первый флашнулся
    await feed(_msg(103, "второй", 2), conv, sender, 0.05)
    await asyncio.sleep(0.12)
    assert [r.text for r in conv.enqueued] == ["первый", "второй"]
