#!/usr/bin/env bash
# Ежедневный дамп базы. Запускается systemd-таймером claude-tg-backup.timer.
#
# Журнал разговоров и история выкаток живут в единственном экземпляре, и
# восстановить их неоткуда: это не кэш, который пересчитается. Дамп сжатый,
# хранится две недели, лежит рядом с проектом и не попадает в git.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="$ROOT/backups"
KEEP_DAYS=14
STAMP="$(date +%Y%m%d-%H%M)"
TARGET="$DIR/tgassistant-$STAMP.sql.gz"

mkdir -p "$DIR"
DSN="$(grep -m1 '^DATABASE_DSN=' "$ROOT/.env" | cut -d= -f2-)"
[ -n "$DSN" ] || { echo "DATABASE_DSN не найден в .env" >&2; exit 1; }

# Дамп пишем во временный файл: прерванный на середине .sql.gz выглядел бы как
# годный бэкап ровно до того дня, когда он понадобится.
pg_dump --no-owner --no-privileges "$DSN" | gzip -9 > "$TARGET.part"
mv "$TARGET.part" "$TARGET"

find "$DIR" -name 'tgassistant-*.sql.gz' -mtime +$KEEP_DAYS -delete
echo "$(date '+%F %T') backup ok $(du -h "$TARGET" | cut -f1) $TARGET"
