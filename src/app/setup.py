"""Мастер первичной настройки прямо в Телеге — залоченный режим.

Запускается скриптом scripts/bootstrap.sh, когда бот ещё не настроен. Умеет
ровно одно: по одноразовому PIN опознать владельца и по кнопкам собрать конфиг
в .env, а на финале — поставить и запустить боевую службу.

Почему это отдельная программа, а не режим основного бота: здесь НЕТ ни процесса
claude, ни shell, ни единого сервиса с доступом к машине. Поэтому запуск без
белого списка тут безопасен — навредить нечем, можно только пройти мастер. Полный
доступ включается лишь после финала, когда служба claude-tg стартует уже с
владельцем в ALLOWED_USER_IDS.

Ключевое правило: .env дописывается ТОЛЬКО на финале (finalize). Если мастер
упал раньше — .env остаётся ненастроенным, и любой случайный старт снова попадёт
в этот же безопасный режим, а не поднимет полноценного бота вслепую.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # чтобы работал и `python src/app/setup.py`

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import CallbackQuery, ForceReply, Message

from app.infrastructure.telegram import ui  # noqa: E402
from app.claude_login import ClaudeLogin, is_logged_in  # noqa: E402

log = logging.getLogger("setup")

ROOT = Path(__file__).resolve().parents[2]
ENV = ROOT / ".env"
SERVICE = "claude-tg"
MAX_PIN_TRIES = 20  # защита от подбора PIN за короткое окно установки


# --------------------------------------------------------------------------- #
#  Чистые помощники (проверяются тестами без Телеграма)
# --------------------------------------------------------------------------- #
def apply_env(text: str, updates: dict[str, str]) -> str:
    """Вернуть текст .env с обновлёнными/добавленными ключами.

    Существующие строки KEY=... перезаписываются на месте (порядок и комментарии
    сохраняются), недостающие ключи дописываются в конец. Это единственное место,
    где мастер меняет конфиг, поэтому оно и покрыто тестом.
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=", line)
        if m and m.group(1) in updates:
            key = m.group(1)
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")
    return "\n".join(out).rstrip("\n") + "\n"


def read_pin() -> str:
    """PIN пишет bootstrap: в run/setup.pin (основное) или в env SETUP_PIN."""
    pin_file = ROOT / "run" / "setup.pin"
    if pin_file.exists():
        return pin_file.read_text(encoding="utf-8").strip()
    return (os.environ.get("SETUP_PIN") or "").strip()


def read_token() -> str:
    m = re.search(r"^TELEGRAM_BOT_TOKEN=(.+)$", ENV.read_text(encoding="utf-8"), re.M)
    return m.group(1).strip() if m else ""


# --------------------------------------------------------------------------- #
#  Состояние мастера (один владелец, один проход)
# --------------------------------------------------------------------------- #
@dataclass
class Wizard:
    pin: str
    owner_id: int | None = None
    pin_tries: int = 0
    # что собрали
    values: dict[str, str] = field(default_factory=dict)
    install_local_stt: bool = False
    # чего ждём текстом (ответ на ForceReply): "llm" | "claude_code" | None
    awaiting: str | None = None
    # авторизация claude
    login: ClaudeLogin | None = None
    claude_authed: bool = False
    # флаг «пора финализировать» — выставляется в хендлере, исполняется в run()
    finalize: bool = False


MODELS = ("opus", "sonnet", "haiku")


def _stt_keyboard():
    return ui.keyboard([ui.button("Облако (OpenRouter)", "setup:stt:cloud", ui.PRIMARY),
                        ui.button("Локально", "setup:stt:local")])


def _llm_keyboard():
    return ui.keyboard([ui.button("Ввести ключ OpenRouter", "setup:llm:key", ui.PRIMARY)],
                       [ui.button("Пропустить", "setup:llm:skip")])


def _model_keyboard():
    return ui.keyboard([ui.button(m, f"setup:model:{m}",
                                  ui.PRIMARY if m == "opus" else ui.DEFAULT) for m in MODELS])


def _auth_keyboard():
    return ui.keyboard([ui.button("Войти в Claude", "setup:auth", ui.PRIMARY)],
                       [ui.button("Пропустить (войду по SSH)", "setup:auth_skip")])


def _finish_keyboard():
    return ui.keyboard([ui.button("Готово, ставь", "setup:finish", ui.SUCCESS)])


def _summary(w: Wizard) -> str:
    stt = "облако (OpenRouter)" if not w.install_local_stt else "локально (faster-whisper)"
    llm = "задан" if w.values.get("LLM_API_KEY") else "нет"
    model = w.values.get("CLAUDE_MODEL", "opus")
    return ("\n".join([
        "Проверь и ставим:",
        f"  распознавание голоса: {stt}",
        f"  ключ OpenRouter (память + облачный STT): {llm}",
        f"  модель Claude: {model}",
    ]))


# --------------------------------------------------------------------------- #
#  Хендлеры
# --------------------------------------------------------------------------- #
def build_router(w: Wizard, dp: Dispatcher) -> Router:
    r = Router(name="setup")

    async def show_stt(msg_target) -> None:
        await msg_target.answer(
            "Ты владелец. Теперь пара вопросов.\n\n"
            "Как распознавать голосовые?\n"
            "  Облако (OpenRouter) — легко, ничего не качается, тем же ключом, что и память.\n"
            "  Локально — без сети, но тяжёлый пакет (~400 МБ) и нагрузка на CPU.",
            reply_markup=_stt_keyboard())

    @r.message(F.text)
    async def on_text(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        text = (message.text or "").strip()

        # --- ещё не опознан владелец: ждём PIN ---------------------------- #
        if w.owner_id is None:
            if text == w.pin:
                w.owner_id = uid
                log.info("owner claimed: %s", uid)
                await show_stt(message)
            else:
                w.pin_tries += 1
                if w.pin_tries >= MAX_PIN_TRIES:
                    log.warning("too many wrong PIN attempts — aborting setup")
                    await message.answer("Слишком много неверных попыток. Мастер остановлен, "
                                         "перезапусти установку на сервере.")
                    asyncio.create_task(dp.stop_polling())
                    return
                await message.answer("Пришли PIN, который напечатан в консоли сервера.")
            return

        # --- дальше слушаем только владельца ------------------------------ #
        if uid != w.owner_id:
            return

        # --- ответ на запрос ключа (ForceReply) --------------------------- #
        if w.awaiting == "llm":
            w.awaiting = None
            if text and text != "-":
                # Один ключ OpenRouter обслуживает чат-вызовы, эмбеддинги и STT
                # (облачное распознавание берёт его же, если STT_API_KEY пуст).
                w.values["LLM_API_KEY"] = text
                w.values["EMBEDDINGS_API_KEY"] = text
            await message.answer("Модель Claude для разговора:", reply_markup=_model_keyboard())
            return
        if w.awaiting == "claude_code":
            w.awaiting = None
            ok = False
            if w.login is not None:
                try:
                    ok = await w.login.submit_code(text)
                except Exception:  # noqa: BLE001
                    ok = False
                w.login = None
            w.claude_authed = ok
            if ok:
                await message.answer("Claude авторизован. Всё, можно ставить.",
                                     reply_markup=_finish_keyboard())
            else:
                await message.answer("Код не принят (просрочен или неверный). Попробуем ещё раз?",
                                     reply_markup=_auth_keyboard())
            return

    @r.callback_query(F.data.startswith("setup:"))
    async def on_button(cb: CallbackQuery) -> None:
        if w.owner_id is None or (cb.from_user and cb.from_user.id != w.owner_id):
            await cb.answer("недоступно")
            return
        data = cb.data or ""

        if data == "setup:stt:cloud":
            # Облачный STT ходит в OpenRouter тем же ключом, что и память, — так что
            # отдельный ключ не спрашиваем, он придёт на следующем шаге. Локального
            # движка нет, поэтому откат на него гасим.
            w.install_local_stt = False
            w.values["LOCAL_STT_FALLBACK"] = "0"
            await cb.message.answer("Голос будет распознавать OpenRouter — тем же ключом, что и "
                                    "смысловая память. Введи ключ OpenRouter (он нужен для обоих).",
                                    reply_markup=_llm_keyboard())
            await cb.answer()
        elif data == "setup:stt:local":
            w.install_local_stt = True
            w.values["LOCAL_STT_FALLBACK"] = "1"
            await cb.message.answer("Служебный ключ OpenRouter? Нужен для смысловой памяти; "
                                    "без него — полнотекстовый поиск.",
                                    reply_markup=_llm_keyboard())
            await cb.answer()
        elif data == "setup:llm:key":
            w.awaiting = "llm"
            await cb.message.answer("Пришли ключ OpenRouter. Или «-», чтобы пропустить.",
                                    reply_markup=ForceReply(input_field_placeholder="sk-or-..."))
            await cb.answer()
        elif data == "setup:llm:skip":
            await cb.message.answer("Модель Claude для разговора:", reply_markup=_model_keyboard())
            await cb.answer()
        elif data.startswith("setup:model:"):
            model = data.rsplit(":", 1)[-1]
            if model in MODELS:
                w.values["CLAUDE_MODEL"] = model
            await cb.message.answer(
                _summary(w) + "\n\nОсталось войти в аккаунт Claude — это подписка, по которой "
                "бот и будет думать. Открою ссылку, а код входа пришлёшь мне обратно.",
                reply_markup=_auth_keyboard())
            await cb.answer()

        elif data == "setup:auth":
            if is_logged_in():
                w.claude_authed = True
                await cb.message.answer("Claude уже авторизован на этом сервере — можно ставить.",
                                        reply_markup=_finish_keyboard())
                await cb.answer()
                return
            await cb.answer("запускаю вход…")
            w.login = ClaudeLogin()
            try:
                url = await w.login.start()
            except Exception as exc:  # noqa: BLE001
                w.login = None
                await cb.message.answer(
                    f"Не смог запустить вход автоматически ({exc}). Зайди на сервер по SSH и "
                    "выполни `claude setup-token`, потом вернись и жми «Готово, ставь».",
                    reply_markup=_finish_keyboard())
                return
            w.awaiting = "claude_code"
            await cb.message.answer(
                "1) Открой ссылку и войди в свой аккаунт Claude:\n" + url
                + "\n\n2) Скопируй код, который там дадут, и пришли его мне сюда одним сообщением.",
                reply_markup=ForceReply(input_field_placeholder="код входа"))

        elif data == "setup:auth_skip":
            w.claude_authed = is_logged_in()
            await cb.message.answer(
                "Ок, вход в Claude сделаешь на сервере командой `claude setup-token`. "
                "Без него бот поднимется, но думать не сможет, пока не авторизуешь.",
                reply_markup=_finish_keyboard())
            await cb.answer()

        elif data == "setup:finish":
            await cb.answer("ставлю…")
            await cb.message.answer("Собираю конфиг и поднимаю боевую службу — минутку.")
            w.finalize = True
            # stop_polling() — корутина: НЕ ждём её внутри хендлера (иначе луп не
            # выйдет и будет дедлок), а планируем задачей. Как только поллинг
            # остановится, run() дойдёт до финала (запись .env + запуск службы).
            asyncio.create_task(dp.stop_polling())

    return r


# --------------------------------------------------------------------------- #
#  Финал: записать .env, доставить локальный STT, включить службу
# --------------------------------------------------------------------------- #
def _finalize(w: Wizard, bot_username: str) -> list[str]:
    """Возвращает список строк-отчётов (что сделал/что не смог) для владельца."""
    report: list[str] = []

    updates = dict(w.values)
    updates["ALLOWED_USER_IDS"] = str(w.owner_id)   # владелец — только здесь, на финале
    ENV.write_text(apply_env(ENV.read_text(encoding="utf-8"), updates), encoding="utf-8")
    ENV.chmod(0o600)
    report.append("конфиг записан в .env")

    if w.install_local_stt:
        try:
            subprocess.run([str(ROOT / "venv" / "bin" / "pip"), "install", "--quiet",
                            "-r", str(ROOT / "requirements-local-stt.txt")],
                           check=True, timeout=1800)
            report.append("локальный движок распознавания установлен")
        except Exception as exc:  # noqa: BLE001
            report.append(f"локальный STT поставить не вышло: {exc}")

    # Служба: тот же путь, что и в install.sh — подставляем реальные пути в юнит.
    try:
        claude_bin = (subprocess.run(["bash", "-lc", "command -v claude || echo /root/.local/bin/claude"],
                                     capture_output=True, text=True).stdout.strip()
                      or "/root/.local/bin/claude")
        unit = (ROOT / "deploy" / f"{SERVICE}.service").read_text(encoding="utf-8")
        unit = (unit.replace("__ROOT__", str(ROOT))
                    .replace("__USER__", os.environ.get("USER", "root"))
                    .replace("__CLAUDE_BIN_DIR__", str(Path(claude_bin).parent)))
        Path(f"/etc/systemd/system/{SERVICE}.service").write_text(unit, encoding="utf-8")
        subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=60)
        subprocess.run(["systemctl", "enable", "--now", SERVICE], check=True, timeout=120)
        report.append("служба claude-tg установлена и запущена")
    except Exception as exc:  # noqa: BLE001
        report.append(f"службу поднять не вышло (сделай вручную scripts/install.sh): {exc}")

    if w.claude_authed or is_logged_in():
        report.append("claude авторизован — бот готов думать")
    else:
        report.append("claude НЕ авторизован: зайди по SSH и выполни `claude setup-token`, "
                      "иначе бот не сможет отвечать")

    # PIN больше не нужен — убираем, чтобы не валялся.
    (ROOT / "run" / "setup.pin").unlink(missing_ok=True)
    return report


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s setup %(message)s")
    token = read_token()
    pin = read_pin()
    if not token:
        raise SystemExit("нет TELEGRAM_BOT_TOKEN в .env — сначала bootstrap.sh")
    if not pin:
        raise SystemExit("нет PIN (run/setup.pin) — сначала bootstrap.sh")

    bot = Bot(token)
    me = await bot.get_me()
    log.info("setup-бот @%s поднят, жду PIN…", me.username)

    w = Wizard(pin=pin)
    dp = Dispatcher()
    dp.include_router(build_router(w, dp))
    try:
        await dp.start_polling(bot, handle_signals=False)
    finally:
        if w.finalize and w.owner_id is not None:
            report = _finalize(w, me.username or "")
            try:
                await bot.send_message(
                    w.owner_id,
                    "Готово:\n" + "\n".join(f"  {line}" for line in report)
                    + "\n\nЕсли служба поднялась — напиши мне ещё раз, отвечу уже я. "
                    "Нужен установленный и авторизованный claude CLI на сервере.")
            except Exception:  # noqa: BLE001
                log.warning("не смог отправить финальный отчёт", exc_info=True)
        await bot.session.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
