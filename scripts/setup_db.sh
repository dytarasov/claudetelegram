#!/usr/bin/env bash
# Провижининг Postgres + pgvector под бота. Идемпотентно: повторный запуск
# ничего не ломает, только освежает пароль роли и синхронизирует его в .env.
#
# Что делает:
#   1. на Debian/Ubuntu доставляет postgresql и расширение pgvector;
#   2. создаёт роль и базу (по умолчанию tgassistant) с новым паролем;
#   3. включает в базе расширение vector;
#   4. ВПИСЫВАЕТ получившийся DATABASE_DSN прямо в .env — чтобы конфиг руками
#      не трогать. Схему (таблицы) накатит уже сам бот при первом старте.
#
# Переменные окружения для тонкой настройки (все необязательны):
#   DB_NAME (tgassistant), DB_USER (tgassistant), DB_HOST (127.0.0.1)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_NAME=${DB_NAME:-tgassistant}
DB_USER=${DB_USER:-tgassistant}
DB_HOST=${DB_HOST:-127.0.0.1}

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m  x %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "провижининг БД требует root: sudo $0"

# --- 1. пакеты -------------------------------------------------------------- #
if command -v apt-get >/dev/null; then
  if ! command -v psql >/dev/null; then
    say "Ставлю PostgreSQL"
    apt-get update -qq
    apt-get install -y -qq postgresql postgresql-contrib
  fi
  # Пакет pgvector именуется под мажорную версию кластера (postgresql-16-pgvector).
  PGVER="$(ls /usr/lib/postgresql 2>/dev/null | sort -V | tail -1 || true)"
  if [[ -n "$PGVER" ]]; then
    apt-get install -y -qq "postgresql-${PGVER}-pgvector" 2>/dev/null \
      || warn "пакет postgresql-${PGVER}-pgvector не нашёлся — поставь pgvector вручную, иначе память бота не заведётся"
  fi
else
  command -v psql >/dev/null || die "нет apt-get и нет psql: поставь PostgreSQL и pgvector вручную, затем пропиши DATABASE_DSN в .env"
fi

command -v pg_lsclusters >/dev/null && pg_lsclusters || true

# --- 2. порт локального кластера -------------------------------------------- #
# Читаем реальный порт у самого сервера, а не угадываем: на чистой машине это
# 5432, но если 5432 занят (у нас его держал контейнер VPN) — кластер мог встать
# на 5433, и DSN обязан это учесть.
PORT="$(su postgres -s /bin/sh -c "psql -tAqX -c 'SHOW port;'" 2>/dev/null | tr -d '[:space:]' || true)"
PORT=${PORT:-5432}
echo "  Postgres слушает порт $PORT"

# --- 3. роль, база, расширение ---------------------------------------------- #
# Пароль — только буквы и цифры: так он безопасен и в SQL-литерале, и в URL DSN
# без всякого экранирования.
PW="$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 28)"

psql_su() { su postgres -s /bin/sh -c "psql -p '$PORT' -d '${2:-postgres}' -tAqX -v ON_ERROR_STOP=1" <<<"$1"; }

say "Роль и база"
if [[ "$(psql_su "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'")" == "1" ]]; then
  psql_su "ALTER ROLE ${DB_USER} WITH LOGIN PASSWORD '${PW}'"
  echo "  роль ${DB_USER} уже была — освежил пароль"
else
  psql_su "CREATE ROLE ${DB_USER} WITH LOGIN PASSWORD '${PW}'"
  echo "  создана роль ${DB_USER}"
fi

if [[ "$(psql_su "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'")" == "1" ]]; then
  echo "  база ${DB_NAME} уже есть"
else
  psql_su "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER}"
  echo "  создана база ${DB_NAME}"
fi

if psql_su "CREATE EXTENSION IF NOT EXISTS vector" "$DB_NAME" >/dev/null 2>&1; then
  echo "  расширение vector включено"
else
  warn "не удалось включить расширение vector — проверь, что pgvector установлен"
fi

# --- 4. DSN в .env ---------------------------------------------------------- #
DSN="postgresql://${DB_USER}:${PW}@${DB_HOST}:${PORT}/${DB_NAME}"
[[ -f "$ROOT/.env" ]] || cp "$ROOT/.env.example" "$ROOT/.env"
chmod 600 "$ROOT/.env"
if grep -q '^DATABASE_DSN=' "$ROOT/.env"; then
  sed -i "s|^DATABASE_DSN=.*|DATABASE_DSN=$DSN|" "$ROOT/.env"
else
  printf '\nDATABASE_DSN=%s\n' "$DSN" >> "$ROOT/.env"
fi

say "Готово"
echo "  DATABASE_DSN записан в .env (порт $PORT, база ${DB_NAME})"
echo "  таблицы бот накатит сам при первом старте"
