"""Экраны: текст плюс клавиатура, собранные в одном месте.

Зачем отдельно от роутеров. Один и тот же экран рисуется дважды — когда его
вызвали командой и когда нажали «Обновить» под уже отправленным сообщением.
Если верстать в обоих местах, они разъезжаются в первый же день. Здесь экран
описан один раз и возвращает пару «текст, клавиатура»; роутер решает только,
отправить это новым сообщением или заменить существующее.

Общие правила вёрстки:
  • ни одного эмодзи — состояние передаётся словом, важность кнопки стилем;
  • длинные простыни (логи, список версий) прячутся в сворачиваемую цитату;
  • необратимое (откат, сброс сессии) всегда через подтверждение;
  • у каждого экрана есть «Обновить» — это дешевле, чем набирать команду снова.
"""
from __future__ import annotations

from datetime import datetime

from aiogram.types import InlineKeyboardMarkup

from ..domain.enums import DeployPhase
from ..infrastructure.telegram import ui
from ..infrastructure.telegram.formatting import esc
from ..infrastructure.telegram.render import fmt_duration, fmt_tokens
from ..services.conversation import ConversationService
from ..services.goals import GoalService
from ..services.health import HealthService
from ..services.logs import LogService
from ..services.proactive import ProactiveService
from ..services.selfupdate import SelfUpdateService
from ..settings import Settings

Screen = tuple[str, InlineKeyboardMarkup]

# Исход выкатки словом: короткое, однозначное, не требует легенды.
PHASE_WORD = {
    DeployPhase.OK: "принята",
    DeployPhase.ROLLED_BACK: "откачена",
    DeployPhase.FAILED: "провалена",
    DeployPhase.APPLYING: "едет",
    DeployPhase.SOAKING: "на выдержке",
    DeployPhase.PENDING: "ждёт",
    DeployPhase.ROLLING_BACK: "откатывается",
}


def _nav(*extra: list) -> InlineKeyboardMarkup:
    """Нижний ряд навигации: возврат в корень и два соседних экрана.

    «Меню» стоит первым и есть везде: из любой точки один шаг до всего
    остального. Без этого кнопочный интерфейс превращается в лабиринт, из
    которого выбираются командой — то есть мимо кнопок.
    """
    return ui.keyboard(*extra, [
        ui.button("Меню", "ui:menu"),
        ui.button("Состояние", "ui:status"),
        ui.button("Диагностика", "ui:diag"),
    ])


async def status(conversation: ConversationService, health: HealthService,
                 stt, settings: Settings) -> Screen:
    st = conversation.process.status()
    used, window = st["ctx_used"], st["ctx_window"]
    snapshot = health.snapshot()

    lines = [
        ui.title("Состояние"),
        "",
        ui.field("процесс", "живой" if st["alive"] else "не запущен"),
        ui.field("аптайм", fmt_duration(st["uptime"])),
        ui.field("версия", (snapshot.commit or "?")[:7], mono=True),
        ui.field("модель", st["model"], mono=True),
        ui.field("директория", st["cwd"], mono=True),
        ui.field("ходов", f"{st['turns']}  ·  ${st['cost']:.2f}"),
        ui.field("контекст", f"{fmt_tokens(used)} / {fmt_tokens(window)}"
                             + (f"  ({used / window * 100:.0f}%)" if window else "")),
        ui.field("голос", stt.backend_name(), mono=True),
        ui.field("журнал", ui.state(snapshot.db_ok)),
    ]
    if conversation.busy:
        lines.append(ui.field("сейчас", f"ход идёт {fmt_duration(conversation.elapsed)}"))
    if conversation.queued:
        lines.append(ui.field("в очереди", str(conversation.queued)))

    keys = _nav([ui.button("Обновить", "ui:status", ui.PRIMARY),
                 ui.button("Прервать ход", "ui:stop", ui.DANGER,
                           disabled=not conversation.busy, why="ход не идёт")])
    return "\n".join(lines), keys


async def diagnostics(health: HealthService, selfupdate: SelfUpdateService) -> Screen:
    s = health.snapshot()
    marker = selfupdate.read_marker() or {}
    beat = s.last_beat_age_s or 999

    lines = [
        ui.title("Диагностика"),
        "",
        ui.field("процесс claude", ui.state(s.claude_alive)),
        ui.field("telegram", ui.state(s.telegram_ok)),
        ui.field("postgres", ui.state(s.db_ok)),
        ui.field("пульс", f"{ui.state(beat < 15)}, {beat:.0f} с назад"),
        ui.field("аптайм", fmt_duration(s.uptime_s)),
        ui.field("версия", (s.commit or "?")[:7], mono=True),
    ]
    if marker:
        lines += ["", ui.title("Последняя выкатка"),
                  ui.field("исход", str(marker.get("phase") or "?")),
                  ui.field("что", str(marker.get("note") or "—"))]
        if marker.get("reason"):
            lines.append(ui.field("причина", str(marker["reason"])))
        if marker.get("revived_in_s"):
            lines.append(ui.field("поднялся за", f"{marker['revived_in_s']:.0f} с"))

    keys = _nav([ui.button("Обновить", "ui:diag", ui.PRIMARY),
                 ui.button("Логи", "ui:logs:30")])
    return "\n".join(lines), keys


async def versions(selfupdate: SelfUpdateService, limit: int = 8) -> Screen:
    items = await selfupdate.versions(limit)
    rows = []
    for v in items:
        marks = []
        if v.is_current:
            marks.append("сейчас")
        if v.is_stable:
            marks.append("stable")
        if v.phase:
            marks.append(PHASE_WORD.get(v.phase, str(v.phase)))
        tail = f"  [{', '.join(marks)}]" if marks else ""
        rows.append(f"{v.short}  {v.subject[:60]}{tail}")
        rows.append(f"        {v.when}")

    current = next((v for v in items if v.is_current), None)
    stable = next((v for v in items if v.is_stable), None)

    text = "\n".join([ui.title("Версии"), "", ui.mono_block("\n".join(rows))])
    actions = []
    if stable and not stable.is_current:
        actions.append(ui.button(f"Откатить на {stable.short}",
                                 f"ui:rollback_ask:{stable.short}", ui.DANGER))
    if current:
        actions.append(ui.copy_button(f"Копировать {current.short}", current.sha))
    return text, _nav(actions, [ui.button("Обновить", "ui:versions", ui.PRIMARY)])


async def todo(proactive: ProactiveService, chat_id: int, settings: Settings) -> Screen:
    items = await proactive.open_items(chat_id)
    if not items:
        return (ui.title("Дел нет") + "\n\n<i>Поставить: /remind через час позвонить</i>",
                ui.keyboard([ui.button("Напомнить о чём-то", "ask:remind", ui.PRIMARY)],
                            [ui.button("Обновить", "ui:todo"),
                             ui.button("Меню", "ui:menu")]))

    now = datetime.now(settings.tz)
    lines = [ui.title(f"Дела: {len(items)}"), f"<i>время по {settings.user_timezone}</i>", ""]
    rows = []
    for item in items:
        local = item.due_at.astimezone(settings.tz)
        late = local <= now
        marks = []
        if item.repeat_rule and item.repeat_rule.startswith("times:"):
            marks.append(item.repeat_rule.removeprefix("times:"))
        if item.fired_count:
            marks.append(f"напоминал {item.fired_count}")
        note = f"  <i>· {' · '.join(marks)}</i>" if marks else ""
        prefix = "просрочено" if late else local.strftime("%d.%m %H:%M")
        lines.append(f"<code>#{item.id}</code> <b>{esc(prefix)}</b> — {esc(item.text)}{note}")
        # По две кнопки на дело: закрыть и отложить. Снять — командой /drop,
        # чтобы случайное касание не стирало задачу безвозвратно.
        rows.append([ui.button(f"Готово {item.id}", f"rem:done:{item.id}", ui.SUCCESS),
                     ui.button(f"Позже {item.id}", f"rem:snooze:{item.id}:60")])

    return "\n".join(lines), ui.keyboard(
        *rows,
        [ui.button("Напомнить о чём-то", "ask:remind", ui.PRIMARY)],
        [ui.button("Обновить", "ui:todo"), ui.button("Меню", "ui:menu")])


async def logs(log_service: LogService, stream: str = "events", limit: int = 30) -> Screen:
    lines = log_service.tail(stream, limit)
    body = "\n".join(lines) if lines else "пусто"
    text = "\n".join([
        ui.title(f"Лог: {stream}"),
        f"<i>последние {len(lines)}</i>",
        "",
        ui.mono_block(body),
    ])
    keys = ui.keyboard(
        [ui.button("Обновить", f"ui:logs:{limit}", ui.PRIMARY),
         ui.button("Ещё 30", f"ui:logs:{min(limit + 30, 200)}")],
        [ui.button("Тревожное", "ui:errors"), ui.button("Файлом", "ui:logfile")],
        [ui.button("Состояние", "ui:status"), ui.button("Диагностика", "ui:diag")],
    )
    return text, keys


async def goals(service: GoalService, chat_id: int) -> Screen:
    items = await service.active(chat_id)
    if not items:
        return (ui.title("Целей нет") + "\n\n<i>Поставить: /goal что сделать | когда закончено</i>",
                ui.keyboard([ui.button("Поставить цель", "ask:goal", ui.PRIMARY)],
                            [ui.button("Обновить", "ui:goals"),
                             ui.button("Меню", "ui:menu")]))

    lines = [ui.title(f"Цели: {len(items)}"), ""]
    rows = []
    for goal in items:
        paused = goal.status.value == "paused"
        lines.append(f"<code>#{goal.id}</code> <b>{esc(goal.text)}</b>"
                     + ("  <i>· на паузе</i>" if paused else ""))
        lines.append(f"        готово, когда: <i>{esc(goal.done_when)}</i>")
        lines.append(f"        шагов {goal.steps_done} · ${goal.spent_usd:.2f} из "
                     f"${goal.budget_usd:.2f} · раз в {goal.cadence_min} мин")
        if goal.last_note:
            lines.append(f"        последнее: <i>{esc(goal.last_note[:120])}</i>")
        rows.append([
            ui.button(("Продолжить " if paused else "Пауза ") + str(goal.id),
                      f"goal:{'resume' if paused else 'pause'}:{goal.id}",
                      ui.SUCCESS if paused else ui.DEFAULT),
            ui.button(f"Снять {goal.id}", f"goal:stop:{goal.id}", ui.DANGER),
        ])
    return "\n".join(lines), ui.keyboard(
        *rows,
        [ui.button("Поставить цель", "ask:goal", ui.PRIMARY)],
        [ui.button("Обновить", "ui:goals"), ui.button("Меню", "ui:menu")])


# --------------------------------------------------------------------------- #
#  Корневое меню и разделы
# --------------------------------------------------------------------------- #
# Смысл этой части: до любого действия можно добраться пальцем, не помня ни
# одной команды. Команды остаются — набрать быстрее, чем ходить по экранам, —
# но знать их наизусть больше не обязательно.
async def menu(conversation: ConversationService, health: HealthService) -> Screen:
    """Корень интерфейса: одна строка о состоянии и шесть разделов."""
    st = conversation.process.status()
    snapshot = health.snapshot()
    busy = "ход идёт" if conversation.busy else "свободен"

    lines = [
        ui.title("Claude Code"),
        "",
        ui.field("сейчас", busy),
        ui.field("модель", st["model"], mono=True),
        ui.field("версия", (snapshot.commit or "?")[:7], mono=True),
    ]
    keys = ui.keyboard(
        [ui.button("Сессия", "ui:session", ui.PRIMARY),
         ui.button("Память", "ui:memory", ui.PRIMARY)],
        [ui.button("Код", "ui:code"), ui.button("Цели", "ui:goals")],
        [ui.button("Напоминания", "ui:todo"), ui.button("Логи", "ui:logs:30")],
        [ui.button("Прервать ход", "ui:stop", ui.DANGER,
                   disabled=not conversation.busy, why="ход не идёт")],
    )
    return "\n".join(lines), keys


async def session(conversation: ConversationService, settings: Settings) -> Screen:
    """Всё про текущую сессию: контекст, модель, директория, перезапуски."""
    st = conversation.process.status()
    used, window = st["ctx_used"], st["ctx_window"]
    share = f"  ({used / window * 100:.0f}%)" if window else ""

    lines = [
        ui.title("Сессия"),
        "",
        ui.field("модель", st["model"], mono=True),
        ui.field("контекст", f"{fmt_tokens(used)} / {fmt_tokens(window)}{share}"),
        ui.field("директория", st["cwd"], mono=True),
        ui.field("ходов", f"{st['turns']}  ·  ${st['cost']:.2f}"),
        ui.field("аптайм", fmt_duration(st["uptime"])),
    ]
    if conversation.busy:
        lines.append(ui.field("сейчас", f"ход идёт {fmt_duration(conversation.elapsed)}"))

    keys = ui.keyboard(
        [ui.button("Обновить", "ui:session", ui.PRIMARY),
         ui.button("Прервать ход", "ui:stop", ui.DANGER,
                   disabled=not conversation.busy, why="ход не идёт")],
        [ui.button("Свернуть контекст", "ui:compact"),
         ui.button("Расход контекста", "ui:ctx")],
        [ui.button("Сменить модель", "ui:models"),
         ui.button("Сменить папку", "ask:cd")],
        [ui.button("Перезапустить", "ui:restart_ask"),
         ui.button("Новая сессия", "ui:new_ask", ui.DANGER)],
        [ui.button("Меню", "ui:menu")],
    )
    return "\n".join(lines), keys


def models(current: str) -> Screen:
    """Выбор модели кнопками: набор известен, значит набирать нечего."""
    known = [("Opus 5", "claude-opus-5"), ("Sonnet 5", "claude-sonnet-5"),
             ("Fable 5", "claude-fable-5"), ("Haiku 4.5", "claude-haiku-4-5-20251001")]
    rows = []
    for label, name in known:
        active = name == current
        rows.append([ui.button(label + (" — сейчас" if active else ""),
                               f"ui:model:{name}",
                               ui.SUCCESS if active else ui.DEFAULT,
                               disabled=active, why="уже выбрана")])
    text = "\n".join([ui.title("Модель"), "",
                      ui.field("сейчас", current, mono=True),
                      "", "<i>смена модели не теряет контекст сессии</i>"])
    return text, ui.keyboard(*rows, [ui.button("Назад", "ui:session")])


async def code(health: HealthService, selfupdate: SelfUpdateService) -> Screen:
    """Раздел самодопила: где мы, что стабильно, чем кончилась выкатка."""
    snapshot = health.snapshot()
    marker = selfupdate.read_marker() or {}
    items = await selfupdate.versions(6)
    stable = next((v for v in items if v.is_stable), None)
    current = next((v for v in items if v.is_current), None)

    lines = [
        ui.title("Код"),
        "",
        ui.field("сейчас", f"{(snapshot.commit or '?')[:7]}"
                           f"  {current.subject[:50] if current else ''}"),
        ui.field("stable", stable.short if stable else "не задан", mono=True),
    ]
    if marker:
        lines += [ui.field("выкатка", str(marker.get("phase") or "?")),
                  ui.field("что", str(marker.get("note") or "—"))]

    actions = []
    if stable and not stable.is_current:
        actions.append(ui.button(f"Откатить на {stable.short}",
                                 f"ui:rollback_ask:{stable.short}", ui.DANGER))
    actions.append(ui.button("Закрепить текущую", "ui:stable_ask", ui.SUCCESS,
                             disabled=bool(current and current.is_stable),
                             why="уже стабильная"))
    keys = ui.keyboard(
        [ui.button("Обновить", "ui:code", ui.PRIMARY),
         ui.button("Версии", "ui:versions")],
        actions,
        [ui.button("Диагностика", "ui:diag"), ui.button("Логи", "ui:logs:30")],
        [ui.button("Меню", "ui:menu")],
    )
    return "\n".join(lines), keys


async def memory(service, journal) -> Screen:
    """Раздел памяти: сколько чего лежит и что с этим можно сделать."""
    stats = await service.stats()
    semantic = stats["semantic"]
    notes = await journal.find_notes("", 100)

    lines = [
        ui.title("Память"),
        "",
        ui.field("факты", f"{stats['facts_active']} действующих"
                          f"  ·  {stats['pinned']} в профиле"),
        ui.field("события", str(stats["events"])),
        ui.field("заметки", str(len(notes))),
        ui.field("журнал", f"{stats['turns'].get('turns', 0)} ходов"),
    ]
    if semantic.get("available"):
        lines.append(ui.field("поиск", f"{semantic['indexed']} из {semantic['total']} кусков"
                                       f"  ·  {semantic['model']}"))
    else:
        lines.append(ui.field("поиск", "только полнотекстовый — эмбеддер не настроен"))

    keys = ui.keyboard(
        [ui.button("Профиль", "ui:profile", ui.PRIMARY),
         ui.button("Найти", "ask:recall", ui.PRIMARY)],
        [ui.button("Записать заметку", "ask:note"),
         ui.button("Заметки", "ui:notes")],
        [ui.button("Что предстоит", "ui:timeline"),
         ui.button("Словарь голоса", "ui:terms")],
        [ui.button("Обновить", "ui:memory"), ui.button("Меню", "ui:menu")],
    )
    return "\n".join(lines), keys


async def profile(service) -> Screen:
    """Закреплённое: то, что я обязан знать всегда."""
    data = await service.profile()
    lines = [ui.title("Профиль"), "", ui.field("сейчас", data["now"]), ""]

    if data["facts"]:
        for fact in data["facts"]:
            mark = "" if fact["source"] == "user" else "  <i>(вывел сам)</i>"
            lines.append(f"• <b>{esc(fact.get('key') or fact['subject'])}</b> — "
                         f"{esc(fact['value'])}{mark}")
    else:
        lines.append("<i>пусто: ни одного закреплённого факта</i>")

    if data["open_commitments"]:
        lines += ["", ui.title("Висит")]
        for item in data["open_commitments"]:
            overdue = "  <b>просрочено</b>" if item["overdue"] else ""
            lines.append(f"• {esc(item['text'])} — {esc(item['due'])}{overdue}")

    keys = ui.keyboard(
        [ui.button("Обновить", "ui:profile", ui.PRIMARY),
         ui.button("Напоминания", "ui:todo")],
        [ui.button("Память", "ui:memory"), ui.button("Меню", "ui:menu")],
    )
    return "\n".join(lines), keys


async def timeline_screen(service, days_back: int = 7, days_ahead: int = 14) -> Screen:
    """События вокруг сегодня — прошедшие и предстоящие одним списком."""
    from datetime import timedelta
    now = datetime.now()
    data = await service.timeline(now - timedelta(days=days_back),
                                  now + timedelta(days=days_ahead))
    lines = [ui.title("События"), "",
             ui.field("окно", f"{data['from']} — {data['to']}"), ""]
    if data["events"]:
        for event in data["events"]:
            when = "предстоит" if event["future"] else "было"
            lines.append(f"• <b>{esc(event['when'])}</b> — {esc(event['title'])}"
                         f"  <i>({when})</i>")
    else:
        lines.append("<i>в этом окне ничего не записано</i>")

    keys = ui.keyboard(
        [ui.button("Обновить", "ui:timeline", ui.PRIMARY),
         ui.button("Шире", "ui:timeline:30:60")],
        [ui.button("Память", "ui:memory"), ui.button("Меню", "ui:menu")],
    )
    return "\n".join(lines), keys


async def notes_screen(journal, limit: int = 15) -> Screen:
    """Заметки: выводы по работе, которые я записывал себе сам."""
    found = await journal.find_notes("", limit)
    lines = [ui.title("Заметки"), ""]
    if found:
        for note in found:
            tags = f"  <i>{esc(', '.join(note.tags))}</i>" if note.tags else ""
            lines.append(f"<b>#{note.id}</b> {esc(note.text[:220])}{tags}")
    else:
        lines.append("<i>пока ни одной</i>")

    keys = ui.keyboard(
        [ui.button("Записать", "ask:note", ui.PRIMARY),
         ui.button("Обновить", "ui:notes")],
        [ui.button("Память", "ui:memory"), ui.button("Меню", "ui:menu")],
    )
    return "\n".join(lines), keys


async def terms_screen(terms) -> Screen:
    """Словарь расшифровки голосовых: что во что превращается."""
    mapping = await terms.mapping()
    lines = [ui.title("Словарь голоса"), ""]
    if mapping:
        rows = [f"{variant} → {canonical}" for variant, canonical in
                sorted(mapping.items())[:60]]
        lines.append(ui.mono_block("\n".join(rows)))
        lines.append(ui.field("всего", str(len(mapping))))
    else:
        lines.append("<i>выучено пусто — работает только затравка из кода</i>")

    keys = ui.keyboard(
        [ui.button("Научить слову", "ask:term", ui.PRIMARY),
         ui.button("Обновить", "ui:terms")],
        [ui.button("Память", "ui:memory"), ui.button("Меню", "ui:menu")],
    )
    return "\n".join(lines), keys
