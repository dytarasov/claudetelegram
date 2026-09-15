#!/usr/bin/env bash
# Базовый установщик: ставит ровно столько, чтобы бот смог написать тебе в Телегу,
# а всё остальное ты донастроишь прямо в чате (мастер app.setup).
#
# Одной командой на чистой машине (Debian/Ubuntu, root):
#   curl -fsSL https://raw.githubusercontent.com/dytarasov/claudetelegram/main/scripts/bootstrap.sh | sudo TOKEN=123:abc bash
#
# Или из клона репозитория:
#   sudo TOKEN=123:abc scripts/bootstrap.sh
#
# Переменные: TOKEN (обязателен) — токен бота от @BotFather;
#             DIR (/root/tgassistant) — куда ставить; REPO_URL — откуда клонировать.
set -euo pipefail

REPO_URL=${REPO_URL:-https://github.com/dytarasov/claudetelegram.git}
DIR=${DIR:-/root/tgassistant}

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m  x %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "нужен root: sudo TOKEN=... $0"

TOKEN=${TOKEN:-}
if [[ -z "$TOKEN" ]]; then
  read -rp "Токен бота от @BotFather: " TOKEN
  [[ -n "$TOKEN" ]] || die "без токена бот не сможет тебе написать"
fi

say "Системные пакеты"
if command -v apt-get >/dev/null; then
  apt-get update -qq
  apt-get install -y -qq git curl ca-certificates python3 python3-venv ffmpeg
else
  warn "apt-get не найден — поставь git, python3-venv, ffmpeg сам"
fi

# --- репозиторий ------------------------------------------------------------ #
# Если скрипт лежит в клоне — работаем в нём; если запущен через curl|bash —
# клонируем сами.
SELF="${BASH_SOURCE[0]:-}"
if [[ -n "$SELF" && -f "$(dirname "$SELF")/../requirements.txt" ]]; then
  ROOT="$(cd "$(dirname "$SELF")/.." && pwd)"
else
  say "Клонирую репозиторий в $DIR"
  [[ -d "$DIR/.git" ]] || git clone --depth 1 "$REPO_URL" "$DIR"
  ROOT="$DIR"
fi
cd "$ROOT"

say "Виртуальное окружение (лёгкая база, без локального whisper)"
[[ -d "$ROOT/venv" ]] || python3 -m venv "$ROOT/venv"
"$ROOT/venv/bin/pip" install --quiet --upgrade pip
"$ROOT/venv/bin/pip" install --quiet -r "$ROOT/requirements.txt"

say "Claude CLI"
# Бинарник ставим сами (это неинтерактивно). Авторизация — отдельный, браузерный
# шаг, его проводит мастер в чате (ссылка → код); тут только кладём сам CLI.
export PATH="$HOME/.local/bin:$PATH"
if command -v claude >/dev/null; then
  echo "  уже есть: $(command -v claude)"
else
  echo "  ставлю claude…"
  curl -fsSL https://claude.ai/install.sh | bash \
    || warn "не смог поставить claude автоматически — позже: curl -fsSL https://claude.ai/install.sh | bash"
  export PATH="$HOME/.local/bin:$PATH"
fi
CLAUDE_BIN="$(command -v claude || echo "$HOME/.local/bin/claude")"

say "Конфигурация"
[[ -f "$ROOT/.env" ]] || cp "$ROOT/.env.example" "$ROOT/.env"
chmod 600 "$ROOT/.env"
# Токен и пути проставляем; ALLOWED_USER_IDS НЕ трогаем — его допишет мастер на
# финале, чтобы до конца настройки бот оставался в безопасном setup-режиме.
python3 - "$ROOT/.env" "$TOKEN" "$CLAUDE_BIN" "$ROOT/workspace" <<'PY'
import re, sys
path, token, claude_bin, workspace = sys.argv[1:5]
text = open(path, encoding="utf-8").read()
def setkv(t, k, v):
    if re.search(rf"^{k}=.*$", t, re.M):
        return re.sub(rf"^{k}=.*$", f"{k}={v}", t, flags=re.M)
    return t.rstrip("\n") + f"\n{k}={v}\n"
text = setkv(text, "TELEGRAM_BOT_TOKEN", token)
text = setkv(text, "CLAUDE_BIN", claude_bin)
text = setkv(text, "WORKSPACE", workspace)
open(path, "w", encoding="utf-8").write(text)
PY
mkdir -p "$ROOT/workspace/uploads" "$ROOT/models" "$ROOT/run"

say "База данных"
"$ROOT/scripts/setup_db.sh" || warn "провижининг БД не удался — можно донастроить позже"

# --- PIN и запуск мастера --------------------------------------------------- #
PIN="$("$ROOT/venv/bin/python" -c 'import secrets; print(secrets.randbelow(900000)+100000)')"
printf '%s\n' "$PIN" > "$ROOT/run/setup.pin"
chmod 600 "$ROOT/run/setup.pin"

# Мастер — залоченная программа: ни claude, ни shell, только сбор конфига.
PYTHONPATH="$ROOT/src" nohup "$ROOT/venv/bin/python" -m app.setup >>"$ROOT/run/setup.log" 2>&1 &

USERNAME="$(curl -s "https://api.telegram.org/bot${TOKEN}/getMe" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin).get("result",{}).get("username",""))' 2>/dev/null || true)"

say "Готово — дальше в Телеге"
echo "  1. Открой бота:  https://t.me/${USERNAME:-<имя_бота>}"
echo "  2. Отправь ему этот PIN: ${PIN}"
echo "  3. Ответь на пару вопросов кнопками — бот сам поставит службу и перейдёт в боевой режим."
echo
echo "  лог мастера: tail -f $ROOT/run/setup.log"
