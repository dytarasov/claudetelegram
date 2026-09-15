#!/usr/bin/env bash
# Сторож самодопила, версия 2.
#
# Единственный компонент, который обязан работать, когда не работает ничего
# другого. Отсюда все его правила:
#
#   • Запускается ТОЛЬКО через systemd-run (см. SystemdClient.spawn_detached).
#     `systemctl restart claude-tg` гасит весь cgroup юнита; сторож, запущенный
#     из процесса бота обычным способом, умер бы вместе с ним — ровно тогда,
#     когда он единственный, кто может вернуть рабочую версию.
#   • Не знает ни про venv, ни про Postgres, ни про один модуль проекта.
#     Только git, systemctl, curl и системный python3 для разбора json.
#   • Всё состояние — в run/update.json. Бот потом сам перенесёт вердикт в БД
#     (SelfUpdateService.sync_from_guard).
#
# Здоровьем считается не «процесс запустился», а пульс с флагом healthy: бот
# дошёл до конца инициализации, видит Telegram и держит живой процесс claude.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN="$ROOT/run"
UPDATE="$RUN/update.json"
HEARTBEAT="$RUN/heartbeat.json"
LOG="$RUN/guard.log"

MODE=deploy; PREV=""; NEW=""; DELAY=8; SOAK=120; HEALTH=90
UNIT="claude-tg"; STABLE_TAG="stable"; MANUAL_STABLE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --prev) PREV="$2"; shift 2 ;;
    --new) NEW="$2"; shift 2 ;;
    --delay) DELAY="$2"; shift 2 ;;
    --soak) SOAK="$2"; shift 2 ;;
    --health) HEALTH="$2"; shift 2 ;;
    --unit) UNIT="$2"; shift 2 ;;
    --stable-tag) STABLE_TAG="$2"; shift 2 ;;
    --manual-stable) MANUAL_STABLE="$2"; shift 2 ;;
    *) shift ;;
  esac
done

mkdir -p "$RUN"
log() { echo "$(date '+%F %T') [$MODE] $*" >> "$LOG"; }

TOKEN="$(grep -m1 '^TELEGRAM_BOT_TOKEN=' "$ROOT/.env" 2>/dev/null | cut -d= -f2- | tr -d ' "'"'"'')"
CHAT="$(python3 -c "
import json
try: print(json.load(open('$UPDATE')).get('chat_id') or '')
except Exception: print('')
" 2>/dev/null)"

notify() {
  log "notify: ${1//$'\n'/ }"
  [ -n "$TOKEN" ] && [ -n "$CHAT" ] || return 0
  # Адрес с токеном уходит в curl через stdin (--config -), а не аргументом:
  # аргументы процесса видны в `ps` любому пользователю машины, и токен бота
  # утекал бы туда на каждое сообщение сторожа.
  printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TOKEN" \
    | curl -sS -m 25 -o /dev/null --config - \
        -d chat_id="$CHAT" -d parse_mode=HTML --data-urlencode text="$1" || true
}

# Записать фазу (и, если есть, причину/время оживания) в run/update.json.
# Бот перенесёт это в таблицу deployments на ближайшем ударе пульса.
mark() {  # mark <фаза> [причина] [секунд_до_оживания]
  python3 - "$UPDATE" "$1" "${2:-}" "${3:-}" <<'PY' 2>/dev/null || true
import datetime, json, sys
path, phase, reason, revived = sys.argv[1:5]
try:
    data = json.load(open(path))
except Exception:
    data = {}
data["phase"] = phase
if reason:
    data["reason"] = reason
if revived:
    data["revived_in_s"] = float(revived)
data["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
json.dump(data, open(path, "w"), ensure_ascii=False, indent=2)
PY
}

short() { git -C "$ROOT" rev-parse --short "$1" 2>/dev/null || echo "${1:0:8}"; }
subject() { git -C "$ROOT" log -1 --format=%s "$1" 2>/dev/null || echo "?"; }

# Пульс свежий, записан после перезапуска и полностью здоровый.
healthy() {  # healthy <момент_рестарта>
  python3 - "$HEARTBEAT" "$1" <<'PY' 2>/dev/null
import json, sys, time
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
ts = float(d.get("ts", 0))
fresh = ts > float(sys.argv[2]) and time.time() - ts < 25
# healthy — флаг новых версий (ready + telegram_ok + claude_alive). У старых
# версий его нет, и тогда довольствуемся ready: иначе откат на предыдущую
# версию выглядел бы как «и она не поднялась», хотя она работает.
ok = d.get("healthy", d.get("ready"))
sys.exit(0 if fresh and ok else 1)
PY
}

# Перезапустить юнит и дождаться здорового пульса. Печатает секунды до оживания.
bring_up() {
  local ts started waited=0
  ts=$(date +%s); started=$ts
  systemctl restart "$UNIT" >/dev/null 2>&1
  while [ "$waited" -lt "$HEALTH" ]; do
    sleep 3; waited=$((waited + 3))
    if healthy "$ts"; then
      REVIVED=$(( $(date +%s) - started ))
      log "ожил за ${REVIVED}с"
      return 0
    fi
  done
  log "не ожил за ${HEALTH}с"
  return 1
}

land_on() {  # переехать на версию и поднять её
  git -C "$ROOT" reset --hard "$1" >/dev/null 2>&1
  log "reset --hard $(short "$1")"
  bring_up
}

REVIVED=0
log "старт: prev=$(short "$PREV") new=$(short "$NEW") delay=${DELAY} soak=${SOAK} health=${HEALTH}"
sleep "$DELAY"

# ---------------------------------------------------------------- откат ---- #
if [ "$MODE" = "rollback" ]; then
  ABANDONED="$(git -C "$ROOT" rev-parse HEAD)"
  git -C "$ROOT" tag -f "rolled/$(date +%Y%m%d-%H%M%S)" "$ABANDONED" >/dev/null 2>&1
  mark rolling_back
  if land_on "$NEW"; then
    mark rolled_back "по команде" "$REVIVED"
    notify "⏪ <b>Откатился</b> на <code>$(short "$NEW")</code> — $(subject "$NEW")
Поднялся за ${REVIVED}с, контекст сессии на месте.
Оставленная версия сохранена меткой <code>rolled/…</code>, не потеряна."
  else
    mark failed "откат не поднялся"
    notify "🆘 <b>Откат не поднялся.</b> Версия <code>$(short "$NEW")</code> не даёт здорового пульса.
Нужен SSH: <code>journalctl -u $UNIT -n 50</code>"
  fi
  exit 0
fi

# -------------------------------------------------------------- выкатка ---- #
mark applying
if bring_up; then
  mark soaking "" "$REVIVED"
  log "выдержка ${SOAK}с"
  waited=0; ok=1
  while [ "$waited" -lt "$SOAK" ]; do
    sleep 5; waited=$((waited + 5))
    if ! healthy 0; then ok=0; log "пульс пропал на ${waited}с выдержки"; break; fi
  done
  if [ "$ok" = "1" ]; then
    if [ "$MANUAL_STABLE" = "1" ]; then
      mark ok "выдержка пройдена, метку ставит человек" "$REVIVED"
      notify "✅ <b>Версия жива</b>: <code>$(short "$NEW")</code>
$(subject "$NEW")
Стабильной не помечал — ты просил решить самому: /stable или /rollback"
    else
      git -C "$ROOT" tag -f "$STABLE_TAG" "$NEW" >/dev/null 2>&1
      mark ok "" "$REVIVED"
      notify "🔒 <b>Версия закреплена стабильной</b>: <code>$(short "$NEW")</code>
$(subject "$NEW")
Поднялась за ${REVIVED}с. Вернуться назад — /rollback"
    fi
    exit 0
  fi
fi

# ------------------------------------------------------- не взлетело ------- #
REASON="не поднялся за ${HEALTH}с"
[ "${ok:-1}" = "0" ] && REASON="умер в первые ${SOAK}с после старта"
log "откат: $REASON"
mark rolling_back "$REASON"
git -C "$ROOT" tag -f "bad/$(date +%Y%m%d-%H%M%S)" "$NEW" >/dev/null 2>&1

if land_on "$PREV"; then
  mark rolled_back "$REASON" "$REVIVED"
  notify "⛔️ <b>Обновление не взлетело</b> — $REASON.
Вернул <code>$(short "$PREV")</code> — $(subject "$PREV")
Бот жив. Сломанная версия помечена <code>bad/…</code> и не потеряна.
Что именно упало: <code>journalctl -u $UNIT -n 30</code>"
  exit 0
fi

STABLE="$(git -C "$ROOT" rev-parse "$STABLE_TAG^{commit}" 2>/dev/null || echo '')"
if [ -n "$STABLE" ] && [ "$STABLE" != "$PREV" ] && land_on "$STABLE"; then
  mark rolled_back "$REASON; прошлая версия тоже не поднялась" "$REVIVED"
  notify "⛔️ <b>Не взлетела ни новая версия, ни прошлая.</b>
Откатил до stable <code>$(short "$STABLE")</code> — бот жив."
  exit 0
fi

mark failed "$REASON; откат не помог"
notify "🆘 <b>Бот не поднимается ни на новой версии, ни на старой.</b>
Нужен SSH: <code>journalctl -u $UNIT -n 50</code>
Вернуть руками: <code>cd $ROOT && git reset --hard $STABLE_TAG && systemctl restart $UNIT</code>"
