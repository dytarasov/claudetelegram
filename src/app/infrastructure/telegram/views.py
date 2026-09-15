"""Отрисовка одного хода разговора в Telegram.

Задача вьюхи: пока claude работает — держать одно «живое» сообщение, которое
обновляется не чаще раза в EDIT_INTERVAL секунд (иначе Telegram начнёт
отбиваться 429); когда ход закончен — заменить его аккуратным разбором:
действия инструментов, текст ответа, футер со временем, токенами и ценой.

Почему это инфраструктура, а не сервис: вьюха ничего не решает. Она получает
поток событий и превращает его в сообщения. Решение «делать ход, писать в
журнал, поднимать упавший процесс» принимает ConversationService.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import Message

from ...settings import Settings
from . import ui
from .formatting import TG_HARD_LIMIT, esc, tail, to_telegram_chunks
from .render import (LIVE_TEXT_TAIL, MAX_LIVE_TOOLS, PACK_LIMIT, TOOL_MARK,
                     fmt_duration, fmt_tokens, pack, tool_html)
from .sender import MessageSender

log = logging.getLogger(__name__)


@dataclass
class Segment:
    """Кусок хода: либо текст ответа, либо вызов инструмента."""

    kind: str  # "text" | "tool"
    text: str = ""
    name: str = ""
    inp: dict = field(default_factory=dict)


class TurnView:
    def __init__(
        self,
        chat_id: int,
        sender: MessageSender,
        settings: Settings,
        context_usage: Callable[[], tuple[int, int]],
        reply_to: int | None = None,
    ) -> None:
        self.chat_id = chat_id
        self.reply_to = reply_to
        self._sender = sender
        self._s = settings
        self._context_usage = context_usage

        self.segments: list[Segment] = []
        self.live_text = ""
        self.thinking = False
        self.tool_count = 0
        self.msg: Message | None = None
        self.started = time.monotonic()
        self._last_edit = 0.0
        self._last_payload = ""
        self._closed = False
        # Режим стрима: "edit" | "draft" | "rich". draft_id должен быть
        # ненулевым и постоянным на весь ход — тогда Telegram анимирует
        # обновления драфта как одно живое сообщение. При первом же отказе
        # метода режим падает в "edit".
        self._mode = settings.stream_mode
        self.draft_id = int(self.started * 1000) % 2_000_000_000 or 1

    # ---- то, что видит сервис (порт TurnPresenter) ----------------------- #
    @property
    def tools(self) -> list[str]:
        return [s.name for s in self.segments if s.kind == "tool"]

    @property
    def answer(self) -> str:
        return "\n\n".join(s.text for s in self.segments if s.kind == "text").strip()

    def answer_markdown(self) -> str:
        """Историческое имя: им пользуется отрисовка внутри этого же файла."""
        return self.answer

    async def start(self) -> None:
        """Показать «работаю» сразу, не дожидаясь первого события от модели."""
        await self.refresh(force=True)

    def close(self) -> None:
        """Прекратить трогать живое сообщение (ход оборвался с ошибкой)."""
        self._closed = True

    # ---- живая часть ----------------------------------------------------- #
    def _live_keyboard(self):
        """Кнопка «Прервать» висит на живом сообщении весь ход: отменить
        генерацию можно одним касанием, не набирая /stop. Тот же колбэк ui:stop,
        что и в меню, — мягкое прерывание с откатом на перезапуск процесса.
        Финальная правка сообщения в finish() уходит без клавиатуры и снимает её."""
        return ui.keyboard([ui.button("Прервать", "ui:stop", ui.DANGER)])

    def _live_html(self) -> str:
        lines: list[str] = []
        tools = [s for s in self.segments if s.kind == "tool"]
        for seg in tools[-MAX_LIVE_TOOLS:]:
            lines.append(tool_html(seg.name, seg.inp))
        if len(tools) > MAX_LIVE_TOOLS:
            lines.insert(0, f"<i>… ещё {len(tools) - MAX_LIVE_TOOLS} действий выше</i>")

        texts = [s.text for s in self.segments if s.kind == "text"]
        if self.live_text:
            texts.append(self.live_text)
        body = "\n".join(t for t in texts if t.strip())
        if body:
            chunks = to_telegram_chunks(tail(body, LIVE_TEXT_TAIL))
            if lines:
                lines.append("")
            lines.append(chunks[-1] if chunks else "")

        status = "<i>думает…</i>" if self.thinking else "<i>работает…</i>"
        elapsed = time.monotonic() - self.started
        if elapsed > 10:
            status += f"  <i>{fmt_duration(elapsed)}</i>"
        lines += ["", status]
        return "\n".join(lines).strip()[:TG_HARD_LIMIT] or status

    async def refresh(self, force: bool = False) -> None:
        if self._closed:
            return
        now = time.monotonic()
        draftish = self._mode in ("draft", "rich")
        # Драфты не бьются об лимит правок, поэтому их можно слать чаще.
        interval = self._s.draft_interval if draftish else self._s.edit_interval
        if not force and now - self._last_edit < interval:
            return
        payload = self._live_html()
        if payload == self._last_payload:
            return
        self._last_edit = now
        self._last_payload = payload

        if draftish:
            send = (self._sender.draft_rich if self._mode == "rich"
                    else self._sender.draft)
            if await send(self.chat_id, self.draft_id, payload):
                return
            # Сервер драфт не принял — дальше живём правками. Сбрасываем снимок,
            # чтобы ближайшая правка точно ушла, а не отсеклась как «то же самое».
            self._mode = "edit"
            self._last_payload = ""

        try:
            if self.msg is None:
                self.msg = await self._sender.bot.send_message(
                    self.chat_id, payload, reply_to_message_id=self.reply_to,
                    reply_markup=self._live_keyboard(),
                )
            else:
                await self._sender.edit(self.chat_id, self.msg.message_id, payload,
                                        reply_markup=self._live_keyboard())
        except TelegramRetryAfter as exc:
            # Не спим в обработчике потока: просто отодвигаем следующую правку.
            self._last_edit = now + exc.retry_after

    # ---- события потока -------------------------------------------------- #
    async def on_event(self, event: dict) -> None:
        etype = event.get("type")

        if etype == "stream_event":
            inner = event.get("event", {})
            itype = inner.get("type")
            if itype == "content_block_start":
                if inner.get("content_block", {}).get("type") == "thinking":
                    self.thinking = True
            elif itype == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta":
                    self.thinking = False
                    self.live_text += delta.get("text", "")
                    await self.refresh()
                elif delta.get("type") == "thinking_delta":
                    self.thinking = True
                    await self.refresh()
            return

        if etype == "assistant":
            # Авторитетный снимок блока: живой хвост больше не нужен.
            self.live_text = ""
            self.thinking = False
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.segments.append(Segment("text", text=block["text"]))
                elif block.get("type") == "tool_use":
                    self.tool_count += 1
                    self.segments.append(
                        Segment("tool", name=block.get("name", "?"), inp=block.get("input") or {})
                    )
            await self.refresh(force=True)
            return

        if etype == "system" and event.get("subtype") == "compact_boundary":
            self.segments.append(Segment("text", text="\n_контекст свёрнут (compact)_\n"))

    # ---- финал ------------------------------------------------------------ #
    def _final_pieces(self) -> list[str]:
        pieces: list[str] = []
        for seg in self.segments:
            if seg.kind == "tool":
                pieces.append(tool_html(seg.name, seg.inp))
            elif seg.text.strip():
                pieces.extend(to_telegram_chunks(seg.text))
        return pieces

    def footer(self, result: dict) -> str:
        used, window = self._context_usage()
        bits = [fmt_duration(time.monotonic() - self.started)]
        if self.tool_count:
            bits.append(f"инструментов {self.tool_count}")
        if window:
            bits.append(f"контекст {fmt_tokens(used)}/{fmt_tokens(window)} "
                        f"({used / window * 100:.0f}%)")
        if result.get("total_cost_usd"):
            bits.append(f"${result['total_cost_usd']:.2f}")
        line = "<i>" + esc(" · ".join(bits)) + "</i>"

        if window and used / window > 0.8:
            line += "\n<i>контекст почти полон — пора /compact</i>"
        if result.get("subtype") == "error_during_execution":
            line = "<i>ход прерван</i>\n" + line
        elif result.get("is_error"):
            line = "<i>ход завершился ошибкой</i>\n" + line
        return line

    async def finish(self, result: dict) -> None:
        self._closed = True
        pieces = self._final_pieces() or ["<i>(ответа нет)</i>"]
        answer = self.answer_markdown()

        # Совсем длинный ответ читать в чате мучительно — отдаём файлом,
        # а в чате оставляем только цепочку действий.
        as_file = len(answer) > self._s.file_fallback_chars
        if as_file:
            pieces = [p for p in pieces if p.startswith(TOOL_MARK)]
            pieces.append("<i>ответ длинный — целиком в файле ниже</i>")

        messages = pack(pieces)
        messages.append(self.footer(result))
        merged: list[str] = []
        for m in messages:  # футер лепим к последнему куску, если влезает
            if merged and len(merged[-1]) + len(m) + 2 <= PACK_LIMIT:
                merged[-1] += "\n\n" + m
            else:
                merged.append(m)

        first = True
        for text in merged:
            if first and self.msg is not None:
                # Правка живого сообщения может упереться во флуд-контроль
                # (TelegramRetryAfter) — особенно если стрим уже часто правил его.
                # Долбить правкой и уж тем более ронять из-за этого весь ход нельзя:
                # под флуд-контролем просто уходим обычным сообщением, а send()
                # сам переждёт retry_after. Иначе финал терялся как «Ошибка хода».
                edited = False
                try:
                    edited = await self._sender.edit(self.chat_id, self.msg.message_id, text)
                except TelegramRetryAfter:
                    edited = False
                if edited:
                    first = False
                    continue
            await self._sender.send(self.chat_id, text)
            first = False

        if as_file:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            await self._sender.send_text_file(self.chat_id, answer, f"answer-{stamp}.md")


class TurnViewFactory:
    """Создатель вьюх — реализация порта TurnPresenterFactory.

    Существует ради одного: ConversationService не должен ничего знать про
    Telegram. Он просит презентера на ход, а кто это будет — решает контейнер.
    """

    def __init__(self, sender: MessageSender, settings: Settings,
                 context_usage: Callable[[], tuple[int, int]]) -> None:
        self._sender = sender
        self._settings = settings
        self._context_usage = context_usage

    def create(self, chat_id: int, reply_to: int | None = None) -> TurnView:
        return TurnView(chat_id, self._sender, self._settings,
                        self._context_usage, reply_to)
