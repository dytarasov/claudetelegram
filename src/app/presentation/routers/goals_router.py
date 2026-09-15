"""Цели: поставить, посмотреть, остановить.

Синтаксис намеренно свободный, но одно требование жёсткое — критерий
завершения. Цель без него не ставится: агент, которому не сказали, что считать
концом, не заканчивает никогда и засчитывает за прогресс любое шевеление.

    /goal привести README в порядок | в нём описаны все команды и слои
    /goal разобрать логи за неделю | найдены и объяснены все WARN · бюджет 2 · каждые 30 мин
"""
from __future__ import annotations

import re

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from dishka.integrations.aiogram import FromDishka

from ...domain.enums import GoalStatus
from ...infrastructure.telegram import ui
from ...infrastructure.telegram.formatting import esc
from ...infrastructure.telegram.sender import MessageSender
from ...services.goals import GoalService
from .. import screens

router = Router(name="goals")

HOWTO = (
    f"{ui.title('Как ставить цель')}\n\n"
    "<code>/goal что сделать | когда считать законченным</code>\n\n"
    "Например:\n"
    "<code>/goal привести README в порядок | в нём описаны все команды и слои</code>\n"
    "<code>/goal разобрать WARN в логах | ни одного необъяснённого · бюджет 2 · каждые 30 мин</code>\n\n"
    "<i>Критерий обязателен: без него я не пойму, когда остановиться. "
    "По умолчанию — бюджет $3, пробуждение раз в час, не больше 8 ходов в сутки, "
    "ночью не работаю, необратимое не делаю без спроса.</i>"
)


def _extract(text: str) -> tuple[str, float | None, int | None]:
    """Вынуть из строки бюджет и ритм, вернув остаток текстом."""
    budget = None
    cadence = None
    match = re.search(r"бюджет\s*\$?\s*(\d+(?:[.,]\d+)?)", text, re.I)
    if match:
        budget = float(match.group(1).replace(",", "."))
        text = text[: match.start()] + text[match.end():]
    match = re.search(r"кажд\w*\s*(\d+)?\s*(мин\w*|час\w*)", text, re.I)
    if match:
        amount = int(match.group(1) or 1)
        cadence = amount if match.group(2).lower().startswith("мин") else amount * 60
        text = text[: match.start()] + text[match.end():]
    return " ".join(text.split()).strip(" ·,-—"), budget, cadence


@router.message(Command("goal"))
async def cmd_goal(message: Message, command: CommandObject,
                   goals: FromDishka[GoalService],
                   sender: FromDishka[MessageSender]) -> None:
    raw = (command.args or "").strip()

    # Управляющие подкоманды: /goal stop 3, /goal pause 3, /goal resume 3
    control = re.fullmatch(r"(stop|pause|resume|done)\s+#?(\d+)", raw, re.I)
    if control:
        action, goal_id = control.group(1).lower(), int(control.group(2))
        status = {"stop": GoalStatus.STOPPED, "pause": GoalStatus.PAUSED,
                  "resume": GoalStatus.ACTIVE, "done": GoalStatus.DONE}[action]
        goal = await goals.set_status(goal_id, status)
        if goal is None:
            await sender.send(message.chat.id, f"Не нашёл цель #{goal_id}.")
            return
        word = {"stop": "снята", "pause": "на паузе", "resume": "снова в работе",
                "done": "закрыта"}[action]
        await sender.send(message.chat.id,
                          f"Цель <code>#{goal_id}</code> {word}.\n<i>{esc(goal.text)}</i>")
        return

    if "|" not in raw:
        await sender.send(message.chat.id, HOWTO)
        return

    body, _, criterion = raw.partition("|")
    body, budget, cadence = _extract(body)
    criterion, crit_budget, crit_cadence = _extract(criterion)
    budget, cadence = budget or crit_budget, cadence or crit_cadence
    if not body or not criterion:
        await sender.send(message.chat.id, HOWTO)
        return

    goal = await goals.create(message.chat.id, body, criterion,
                              cadence_min=cadence, budget_usd=budget)
    await sender.send(message.chat.id, "\n".join([
        ui.title(f"Цель #{goal.id} принята"),
        "",
        ui.field("что", goal.text),
        ui.field("закончена, когда", goal.done_when),
        ui.field("ритм", f"раз в {goal.cadence_min} мин"),
        ui.field("бюджет", f"${goal.budget_usd:.2f}"),
        ui.field("потолок", f"{goal.max_turns_day} ходов в сутки"),
        "",
        "<i>Первый заход — сейчас. Каждый шаг увидишь в этом чате.</i>",
    ]), reply_markup=ui.keyboard([
        ui.button("Цели", "ui:goals", ui.PRIMARY),
        ui.button("Пауза", f"goal:pause:{goal.id}"),
        ui.button("Снять", f"goal:stop:{goal.id}", ui.DANGER),
    ]))


@router.message(Command("goals"))
async def cmd_goals(message: Message, goals: FromDishka[GoalService],
                    sender: FromDishka[MessageSender]) -> None:
    text, keys = await screens.goals(goals, message.chat.id)
    await sender.send(message.chat.id, text, reply_markup=keys)
