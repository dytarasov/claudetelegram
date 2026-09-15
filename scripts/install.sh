#!/usr/bin/env bash
# Разворачивает бота на чистой машине: venv, зависимости, БД, .env, systemd-юнит.
# Запускать можно повторно — уже сделанные шаги пропускаются.
#
#   ./scripts/install.sh                 полная установка: БД + служба
#   ./scripts/install.sh --no-db         не трогать Postgres (DSN пропишешь сам)
#   ./scripts/install.sh --no-service    только venv, БД и .env, без службы
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=claude-tg
UNIT=/etc/systemd/system/$SERVICE.service
WITH_SERVICE=1
WITH_DB=1
for arg in "$@"; do
  case "$arg" in
    --no-service) WITH_SERVICE=0 ;;
    --no-db)      WITH_DB=0 ;;
    *) echo "неизвестный флаг: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m  x %s\033[0m\n' "$*" >&2; exit 1; }

say "Проверяю окружение"

command -v python3 >/dev/null || die "нужен python3 (3.10+): apt install python3 python3-venv"
python3 - <<'PY' || die "нужен python 3.10 или новее"
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY
python3 -c 'import venv' 2>/dev/null || die "нет модуля venv: apt install python3-venv"
echo "  python $(python3 -V | cut -d' ' -f2)"

command -v ffmpeg >/dev/null \
  && echo "  ffmpeg есть" \
  || warn "ffmpeg не найден — голосовые расшифровываться не будут: apt install ffmpeg"

CLAUDE_BIN="$(command -v claude || true)"
[[ -z "$CLAUDE_BIN" && -x "$HOME/.local/bin/claude" ]] && CLAUDE_BIN="$HOME/.local/bin/claude"
if [[ -n "$CLAUDE_BIN" ]]; then
  echo "  claude: $CLAUDE_BIN"
else
  warn "claude CLI не найден. Поставь и авторизуйся до запуска бота:"
  warn "    curl -fsSL https://claude.ai/install.sh | bash && claude"
  CLAUDE_BIN="$HOME/.local/bin/claude"
fi

say "Виртуальное окружение"
[[ -d "$ROOT/venv" ]] || python3 -m venv "$ROOT/venv"
"$ROOT/venv/bin/pip" install --quiet --upgrade pip
"$ROOT/venv/bin/pip" install --quiet -r "$ROOT/requirements.txt"
echo "  зависимости из requirements.txt установлены"

say "Конфигурация"
if [[ -f "$ROOT/.env" ]]; then
  echo "  .env уже есть — не трогаю"
else
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "  создан .env из .env.example"
fi
chmod 600 "$ROOT/.env"
sed -i "s|^CLAUDE_BIN=.*|CLAUDE_BIN=$CLAUDE_BIN|; s|^WORKSPACE=.*|WORKSPACE=$ROOT/workspace|" "$ROOT/.env"
mkdir -p "$ROOT/workspace/uploads" "$ROOT/models"

# --- база данных ------------------------------------------------------------ #
# Отдельный скрипт: ставит Postgres+pgvector, заводит роль/базу и вписывает
# DATABASE_DSN в .env. Без root провижинить нельзя — тогда просим сделать вручную.
if [[ $WITH_DB -eq 1 ]]; then
  if [[ $EUID -eq 0 ]]; then
    say "База данных"
    "$ROOT/scripts/setup_db.sh" || warn "провижининг БД не удался — пропиши DATABASE_DSN в .env сам"
  else
    warn "БД пропущена (нужен root). Позже: sudo $ROOT/scripts/setup_db.sh"
  fi
fi

token=$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "$ROOT/.env")
users=$(sed -n 's/^ALLOWED_USER_IDS=//p' "$ROOT/.env")

if [[ -z "$token" ]]; then
  say "Что осталось сделать руками"
  echo "  1. Создай бота у @BotFather и впиши токен:"
  echo "       nano $ROOT/.env      # TELEGRAM_BOT_TOKEN=..."
  echo "  2. Узнай свой Telegram ID (скрипт ждёт твоё сообщение боту):"
  echo "       $ROOT/venv/bin/python $ROOT/scripts/whoami.py"
  echo "  3. Повтори: $ROOT/scripts/install.sh"
  exit 0
fi

if [[ -z "$users" ]]; then
  say "Определяю владельца"
  echo "  напиши боту любое сообщение — его автор попадёт в ALLOWED_USER_IDS"
  "$ROOT/venv/bin/python" "$ROOT/scripts/whoami.py" \
    || die "белый список пуст, а без него бот не стартует"
fi

if [[ $WITH_SERVICE -eq 0 ]]; then
  say "Готово (без службы)"
  echo "  запуск вручную: $ROOT/venv/bin/python $ROOT/src/bot.py"
  exit 0
fi

command -v systemctl >/dev/null || die "нет systemd — запускай вручную: $ROOT/venv/bin/python $ROOT/src/bot.py"
[[ $EUID -eq 0 ]] || die "установка службы требует root: sudo $0"

say "Служба systemd"
sed -e "s|__ROOT__|$ROOT|g" \
    -e "s|__USER__|$(id -un)|g" \
    -e "s|__CLAUDE_BIN_DIR__|$(dirname "$CLAUDE_BIN")|g" \
    "$ROOT/deploy/$SERVICE.service" > "$UNIT"
chmod 644 "$UNIT"
systemctl daemon-reload
systemctl enable --now "$SERVICE"
sleep 2
systemctl is-active --quiet "$SERVICE" \
  && echo "  $SERVICE запущен" \
  || warn "$SERVICE не поднялся — смотри: journalctl -u $SERVICE -n 50"

say "Готово"
echo "  логи:       journalctl -u $SERVICE -f"
echo "  рестарт:    systemctl restart $SERVICE"
echo "  напиши боту в Telegram — сессия начнётся с чистого листа"
