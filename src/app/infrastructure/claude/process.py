"""Долгоживущий процесс Claude Code — реализация порта AssistantProcess.

Процесс `claude --print --input-format stream-json` не завершается после ответа:
пока stdin открыт, он ждёт следующее сообщение и держит весь контекст. Это и есть
«одна большая сессия» — бот только подаёт реплики и разбирает поток событий.

Почему это инфраструктура, а не сервис: здесь нет ни одного решения о том, что
делать с ответом, — только запуск процесса, разбор потока и передача событий
наверх. Всё, что «зачем», живёт в services/conversation.py.

Отдельно про --resume: id сессии переживает перезапуск бота, поэтому обновление
кода не стирает разговор. Но резюмировать можно только то, что есть на диске,
иначе claude падает с «No conversation found» — отсюда проверка транскрипта в
_build_args и повтор хода с чистой сессией в ask().
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from ...domain.errors import SessionDead

log = logging.getLogger("claude")

# Строка потока может быть огромной (результат Read большого файла).
STREAM_LIMIT = 64 * 1024 * 1024

EventCb = Callable[[dict], Awaitable[None]]


class ClaudeProcess:
    def __init__(
        self,
        claude_bin: str,
        cwd: Path,
        model: str,
        permission_mode: str,
        session_id: str | None = None,
        mcp_config: Path | None = None,
    ) -> None:
        self.claude_bin = claude_bin
        self.cwd = Path(cwd)
        self.model = model
        self.permission_mode = permission_mode
        self.session_id = session_id
        # Конфиг MCP-серверов: через него подключается память. Необязателен —
        # без него сессия просто живёт без инструментов памяти.
        self.mcp_config = mcp_config

        self.proc: asyncio.subprocess.Process | None = None
        self.init_info: dict[str, Any] = {}
        self.started_at: float | None = None
        self.last_result: dict[str, Any] = {}
        self.rate_limit: dict[str, Any] = {}
        self.total_cost = 0.0
        self.turn_count = 0
        self.stderr_tail: list[str] = []

        self._events: asyncio.Queue[dict] | None = None
        self._lock = asyncio.Lock()
        self._reader: asyncio.Task | None = None
        self._stderr_reader: asyncio.Task | None = None
        self._pending_ctl: dict[str, asyncio.Future] = {}
        self._ctl_seq = 0
        self._ready = asyncio.Event()

    # ------------------------------------------------------------------ #
    #  жизненный цикл
    # ------------------------------------------------------------------ #
    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def _build_args(self) -> list[str]:
        args = [
            self.claude_bin,
            "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode", self.permission_mode,
            "--model", self.model,
        ]
        if self.mcp_config and self.mcp_config.exists():
            args += ["--mcp-config", str(self.mcp_config)]
        if self.session_id and self._transcript().exists():
            # Тот же id — тот же контекст, даже если бот перезапускался.
            args += ["--resume", self.session_id]
        else:
            if self.session_id:
                # Резюмировать нечего: claude такой id не знает и сразу упадёт
                # («No conversation found»), поэтому начинаем чистую сессию.
                log.warning("сессия %s не найдена на диске — начинаю новую", self.session_id)
            self.session_id = str(uuid.uuid4())
            args += ["--session-id", self.session_id]
        return args

    def _transcript(self) -> Path:
        """Файл разговора: ~/.claude/projects/<путь-через-дефисы>/<id>.jsonl.

        Пустая сессия появляется на диске только после первого хода, поэтому её
        наличие — единственный надёжный признак, что --resume сработает.
        """
        home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(self.cwd))
        return home / "projects" / slug / f"{self.session_id}.jsonl"

    async def start(self) -> None:
        if self.alive:
            return
        self.cwd.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        # claude отказывается работать под root с bypassPermissions без этого флага.
        env["IS_SANDBOX"] = "1"
        # Иначе вложенный запуск путается, если бот сам стартовал из-под Claude Code.
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
            env.pop(k, None)

        args = self._build_args()
        log.info("старт claude: %s (cwd=%s)", " ".join(args[1:]), self.cwd)
        self.proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.cwd),
            env=env,
            limit=STREAM_LIMIT,
        )
        self.started_at = time.time()
        self._ready.clear()
        self._reader = asyncio.create_task(self._read_stdout())
        self._stderr_reader = asyncio.create_task(self._read_stderr())

    def set_cwd(self, path: Path) -> None:
        """Применится при следующем старте: аргументы процесса задаются один раз."""
        self.cwd = Path(path)

    def set_model(self, name: str) -> None:
        self.model = name

    async def stop(self) -> None:
        proc, self.proc = self.proc, None
        for task in (self._reader, self._stderr_reader):
            if task:
                task.cancel()
        self._reader = self._stderr_reader = None
        if proc and proc.returncode is None:
            try:
                if proc.stdin and not proc.stdin.is_closing():
                    proc.stdin.close()
                await asyncio.wait_for(proc.wait(), timeout=10)
            except (asyncio.TimeoutError, ProcessLookupError, BrokenPipeError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

    async def restart(self, *, fresh: bool = False) -> None:
        """fresh=True начинает новую сессию (новый контекст с нуля)."""
        await self.stop()
        if fresh:
            self.session_id = None
            self.total_cost = 0.0
            self.turn_count = 0
            self.last_result = {}
            self.init_info = {}
        await self.start()

    # ------------------------------------------------------------------ #
    #  чтение потока
    # ------------------------------------------------------------------ #
    async def _read_stdout(self) -> None:
        proc = self.proc
        assert proc and proc.stdout
        cancelled = False
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    log.warning("строка длиннее лимита потока — пропущена")
                    continue
                if not line:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("не-JSON в потоке: %.200s", line)
                    continue
                await self._dispatch(event)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            log.exception("сбой читателя stdout")
        finally:
            if not cancelled:  # при штатной остановке шуметь не о чем
                log.warning("поток claude закрыт (rc=%s)", proc.returncode)
                if self._events is not None:
                    await self._events.put({"type": "__dead__"})

    async def _read_stderr(self) -> None:
        proc = self.proc
        assert proc and proc.stderr
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    log.warning("claude stderr: %s", text)
                    self.stderr_tail.append(text)
                    del self.stderr_tail[:-20]
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _dispatch(self, event: dict) -> None:
        etype = event.get("type")

        if etype == "control_response":
            resp = event.get("response", {})
            fut = self._pending_ctl.pop(resp.get("request_id", ""), None)
            if fut and not fut.done():
                fut.set_result(resp)
            return

        if etype == "system" and event.get("subtype") == "init":
            self.init_info = event
            # id мог смениться (например, при форке) — держимся за актуальный.
            if event.get("session_id"):
                self.session_id = event["session_id"]
            self._ready.set()
        elif etype == "rate_limit_event":
            self.rate_limit = event.get("rate_limit_info", {})
        elif etype == "result":
            self.last_result = event
            self.total_cost = event.get("total_cost_usd") or self.total_cost
            self.turn_count += 1

        if self._events is not None:
            await self._events.put(event)

    # ------------------------------------------------------------------ #
    #  реплики
    # ------------------------------------------------------------------ #
    def _write(self, payload: dict) -> None:
        if not self.alive or not self.proc or not self.proc.stdin:
            raise SessionDead("процесс claude не запущен")
        self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())

    async def ask(self, text: str, on_event: EventCb) -> dict:
        """Отправляет реплику и скармливает события колбэку до события result.

        Возвращает событие result. Ходы сериализованы: пока идёт один, следующий
        ждёт на замке, поэтому вывод не перемешивается.
        """
        async with self._lock:
            for attempt in (0, 1):
                try:
                    return await self._ask_once(text, on_event)
                except SessionDead:
                    lost = any(
                        "No conversation found" in line for line in self.stderr_tail[-5:]
                    )
                    if attempt or not lost:
                        raise
                    log.warning(
                        "claude потерял сессию %s — начинаю новую и повторяю ход",
                        self.session_id,
                    )
                    await self.stop()
                    self.session_id = None
                    self.stderr_tail.clear()
                    self.init_info = {}
            raise SessionDead("claude не запустился")

    async def _ask_once(self, text: str, on_event: EventCb) -> dict:
        if not self.alive:
            await self.start()

        queue: asyncio.Queue[dict] = asyncio.Queue()
        self._events = queue
        try:
            try:
                self._write(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": text},
                    }
                )
                await self.proc.stdin.drain()  # type: ignore[union-attr]
            except (BrokenPipeError, ConnectionResetError, SessionDead) as exc:
                raise SessionDead(f"не удалось отправить реплику: {exc}") from exc

            while True:
                event = await queue.get()
                if event.get("type") == "__dead__":
                    raise SessionDead(
                        "claude завершился: "
                        + (" | ".join(self.stderr_tail[-3:]) or "без сообщения")
                    )
                await on_event(event)
                if event.get("type") == "result":
                    return event
        finally:
            self._events = None

    async def interrupt(self, timeout: float = 15.0) -> bool:
        """Мягкая отмена текущего хода через control-протокол."""
        if not self.alive:
            return False
        self._ctl_seq += 1
        request_id = f"tg-int-{self._ctl_seq}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_ctl[request_id] = fut
        try:
            self._write(
                {
                    "type": "control_request",
                    "request_id": request_id,
                    "request": {"subtype": "interrupt"},
                }
            )
            await self.proc.stdin.drain()  # type: ignore[union-attr]
            resp = await asyncio.wait_for(fut, timeout)
            return resp.get("subtype") == "success"
        except (asyncio.TimeoutError, SessionDead, BrokenPipeError):
            return False
        finally:
            self._pending_ctl.pop(request_id, None)

    # ------------------------------------------------------------------ #
    #  состояние
    # ------------------------------------------------------------------ #
    def context_usage(self) -> tuple[int, int]:
        """(занято токенов, размер окна) по последнему result-событию.

        Реальный размер контекста — это последняя итерация хода: свежий ввод плюс
        всё, что прочиталось/записалось в кэш. Сумма по всем итерациям завысила бы.
        """
        result = self.last_result
        if not result:
            return 0, 0
        usage = result.get("usage") or {}
        iterations = usage.get("iterations") or []
        src = iterations[-1] if iterations else usage
        used = (
            (src.get("input_tokens") or 0)
            + (src.get("cache_creation_input_tokens") or 0)
            + (src.get("cache_read_input_tokens") or 0)
        )
        model_usage = result.get("modelUsage") or {}
        window = 0
        main_model = self.init_info.get("model")
        if main_model and main_model in model_usage:
            window = model_usage[main_model].get("contextWindow") or 0
        if not window and model_usage:
            window = max(
                (m.get("contextWindow") or 0) for m in model_usage.values()
            )
        return used, window

    def status(self) -> dict[str, Any]:
        used, window = self.context_usage()
        return {
            "alive": self.alive,
            "pid": self.proc.pid if self.proc else None,
            "session_id": self.session_id,
            "cwd": str(self.cwd),
            "model": self.init_info.get("model") or self.model,
            "permission_mode": self.permission_mode,
            "uptime": time.time() - self.started_at if self.started_at else 0,
            "turns": self.turn_count,
            "cost": self.total_cost,
            "ctx_used": used,
            "ctx_window": window,
            "rate_limit": self.rate_limit,
        }
