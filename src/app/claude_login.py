"""Авторизация claude CLI через чат: запускаем `claude setup-token`, отдаём
пользователю ссылку, принимаем код обратно.

`claude setup-token` — интерактивный Ink-TUI: печатает ссылку вида
https://claude.com/cai/oauth/authorize?... и ждёт у промпта «Paste code here».
TUI требует настоящий терминал, поэтому запускаем его в PTY и общаемся через
мастер-конец. Ширину терминала ставим большой, чтобы длинный URL не переносился
на несколько строк и его можно было выдернуть одним куском.

Тут только механика диалога с процессом. Что показать в Телеге и когда — решает
мастер (app.setup). Разбор URL вынесен в чистую функцию: её проверяет тест, не
поднимая ни PTY, ни сети.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import select
import shutil
import struct
import subprocess
import time


def claude_bin() -> str:
    """Путь к claude CLI. PATH может не включать ~/.local/bin (мастер запущен не
    из systemd), поэтому ищем и там — иначе setup-token падает 'No such file'."""
    return shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][AB0]")
# Ссылка входа. Ограничиваем набор символов теми, что реально бывают в URL, —
# так `[^...]+` не захватит хвост промпта, если пробел/перенос вдруг потеряется.
_URL = re.compile(r"https://claude\.com/[A-Za-z0-9%._~:/?#\[\]@!$&'()*+,;=\-]+")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text).replace("\r", "")


def extract_url(text: str) -> str | None:
    """Выдернуть ссылку входа из (возможно, замусоренного ANSI) вывода TUI."""
    m = _URL.search(strip_ansi(text))
    return m.group(0) if m else None


def is_logged_in() -> bool:
    """Спросить сам CLI, залогинены ли мы. `claude auth status` печатает JSON."""
    try:
        out = subprocess.run([claude_bin(), "auth", "status"], capture_output=True,
                             text=True, timeout=30).stdout
        return bool(json.loads(out).get("loggedIn"))
    except Exception:  # noqa: BLE001 — нет ответа/не JSON = считаем «не залогинены»
        return False


class ClaudeLogin:
    """Живой процесс `claude setup-token` под PTY. Один вход — один экземпляр."""

    def __init__(self, extra_env: dict[str, str] | None = None) -> None:
        self._proc: subprocess.Popen | None = None
        self._master: int | None = None
        self._buf = ""
        self._env = {**os.environ, **(extra_env or {})}

    def _spawn(self) -> None:
        import pty
        import termios

        master, slave = pty.openpty()
        # 200 строк x 1000 колонок: с запасом, чтобы URL не переносился.
        with_size = struct.pack("HHHH", 200, 1000, 0, 0)
        import fcntl
        fcntl.ioctl(slave, termios.TIOCSWINSZ, with_size)
        self._proc = subprocess.Popen(
            [claude_bin(), "setup-token"],
            stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True, env=self._env, close_fds=True,
        )
        os.close(slave)
        self._master = master

    def _read_for(self, url_deadline: float) -> str | None:
        """Читать мастер-конец, пока не появится URL или не выйдет время."""
        while time.time() < url_deadline and self._master is not None:
            r, _, _ = select.select([self._master], [], [], 0.5)
            if not r:
                continue
            try:
                chunk = os.read(self._master, 65536)
            except OSError:
                break
            if not chunk:
                break
            self._buf += chunk.decode("utf-8", "replace")
            url = extract_url(self._buf)
            if url:
                return url
        return extract_url(self._buf)

    async def start(self, timeout: float = 40) -> str:
        """Запустить процесс и вернуть ссылку входа. Бросает RuntimeError, если её нет."""
        await asyncio.to_thread(self._spawn)
        url = await asyncio.to_thread(self._read_for, time.time() + timeout)
        if not url:
            self.close()
            raise RuntimeError("не удалось получить ссылку входа от claude setup-token")
        return url

    def _submit(self, code: str, deadline: float) -> bool:
        assert self._master is not None and self._proc is not None
        os.write(self._master, (code.strip() + "\n").encode())
        # Дочитываем до выхода процесса: код принят — токен сохранён, CLI завершится.
        while time.time() < deadline:
            if self._proc.poll() is not None:
                break
            r, _, _ = select.select([self._master], [], [], 0.5)
            if r:
                try:
                    self._buf += os.read(self._master, 65536).decode("utf-8", "replace")
                except OSError:
                    break
        return is_logged_in()

    async def submit_code(self, code: str, timeout: float = 60) -> bool:
        """Отдать код процессу и подтвердить успех через `claude auth status`."""
        try:
            return await asyncio.to_thread(self._submit, code, time.time() + timeout)
        finally:
            self.close()

    def close(self) -> None:
        if self._master is not None:
            try:
                os.close(self._master)
            except OSError:
                pass
            self._master = None
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        self._proc = None
