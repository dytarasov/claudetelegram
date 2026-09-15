"""Разбор человеческого времени: «через 20 минут», «завтра в 10», «каждый день в 9».

Два пути, в таком порядке:

  1. Регулярки на частые случаи. Мгновенно, бесплатно, работает без сети и
     предсказуемо. «через 20 минут» не тот случай, ради которого стоит ходить
     во внешнюю модель.
  2. Языковая модель — на всё остальное: «в следующий вторник после обеда»,
     «когда закончится выкатка», «по будням в девять утра». Она возвращает
     строгий JSON, который мы проверяем, а не исполняем на веру.

Если не смог ни тот, ни другой — честно говорим «не понял», а не назначаем
что-нибудь наугад. Напоминание, сработавшее не тогда, хуже несработавшего:
первое учит не доверять, второе просто просят повторить.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..domain.ports import LLMClient

log = logging.getLogger(__name__)

WEEKDAYS = {"понедельник": 0, "вторник": 1, "среда": 2, "среду": 2, "четверг": 3,
            "пятница": 4, "пятницу": 4, "суббота": 5, "субботу": 5,
            "воскресенье": 6, "пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}

UNITS = {
    "минут": 60, "минуту": 60, "минуты": 60, "мин": 60, "м": 60,
    "час": 3600, "часа": 3600, "часов": 3600, "ч": 3600,
    "день": 86400, "дня": 86400, "дней": 86400, "д": 86400,
    "недел": 604800,
}


@dataclass(slots=True)
class Schedule:
    """Когда сработать и надо ли повторять."""

    at: datetime
    repeat: str | None = None
    text: str = ""


def _round_up(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)


def _first_of(moments: list[str], now: datetime, tomorrow: bool = False) -> datetime:
    """Ближайшее из перечисленных времён суток."""
    day = now + timedelta(days=1) if tomorrow else now
    for moment in moments:
        hour, minute = (int(x) for x in moment.split(":"))
        candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > now:
            return candidate
    hour, minute = (int(x) for x in moments[0].split(":"))
    return (day + timedelta(days=1)).replace(hour=hour, minute=minute,
                                             second=0, microsecond=0)


def parse_quick(phrase: str, now: datetime) -> Schedule | None:
    """Частые формы — без обращения к модели."""
    text = phrase.strip().lower()

    # «через 20 минут», «через 2 часа»
    match = re.search(r"через\s+(\d+)\s*([а-яё]+)", text)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        for prefix, seconds in UNITS.items():
            if unit.startswith(prefix):
                return Schedule(_round_up(now + timedelta(seconds=amount * seconds)))

    # «в 11, 15 и 19», «в 11 15 19» — несколько раз за день.
    # «3 раза» отсюда надо выкинуть заранее: это количество, а не время, и
    # именно на нём разбор ломался, назначая напоминание на три часа ночи.
    without_counts = re.sub(r"\d+\s*раз\w*", " ", text)
    times = re.findall(r"(?:^|\bв\b|,|\bи\b)\s*(\d{1,2})(?::(\d{2}))?\s*(?:час\w*)?",
                       without_counts)
    if len(times) >= 3:
        moments = sorted({f"{int(h):02d}:{m or '00'}" for h, m in times if int(h) <= 23})
        if len(moments) >= 3:
            first = _first_of(moments, now, tomorrow="завтра" in text)
            return Schedule(first, repeat="times:" + ",".join(moments))

    # «каждый день в 9», «ежедневно в 09:30»
    match = re.search(r"(?:кажд\w+\s+день|ежедневно)\s*(?:в\s*)?(\d{1,2})(?::(\d{2}))?", text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        first = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if first <= now:
            first += timedelta(days=1)
        return Schedule(first, repeat="daily")

    # «завтра в 10», «сегодня в 18:30»
    match = re.search(r"(завтра|сегодня|послезавтра)\s*(?:в\s*)?(\d{1,2})(?::(\d{2}))?", text)
    if match:
        shift = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[match.group(1)]
        hour, minute = int(match.group(2)), int(match.group(3) or 0)
        return Schedule((now + timedelta(days=shift)).replace(
            hour=hour, minute=minute, second=0, microsecond=0))

    # «в 15:00» — ближайшее такое время
    match = re.fullmatch(r"(?:в\s*)?(\d{1,2}):(\d{2})", text)
    if match:
        target = now.replace(hour=int(match.group(1)), minute=int(match.group(2)),
                             second=0, microsecond=0)
        return Schedule(target if target > now else target + timedelta(days=1))

    return None


PROMPT = """Ты разбираешь время из русской фразы. Сейчас {now} ({weekday}).
Верни ТОЛЬКО JSON без пояснений:
{{"at": "YYYY-MM-DD HH:MM", "repeat": null|"daily"|"weekly:mon", "text": "суть напоминания"}}
Если времени в фразе нет вовсе — верни {{"at": null}}.
Фраза: {phrase}"""


class WhenParser:
    """Разбор времени: сначала regexp, потом модель."""

    def __init__(self, llm: LLMClient, settings) -> None:
        self._llm = llm
        self._s = settings

    async def parse(self, phrase: str, now: datetime | None = None) -> Schedule | None:
        # Считаем в поясе человека: «в 11» — это одиннадцать по его часам,
        # а не по серверным. Результат остаётся абсолютным моментом времени.
        now = (now or datetime.now(self._s.tz)).astimezone(self._s.tz)
        quick = parse_quick(phrase, now)
        if quick:
            quick.text = self._strip_time_words(phrase)
            return quick
        if not self._llm.available():
            return None
        try:
            reply = await self._llm.complete(
                PROMPT.format(now=now.strftime("%Y-%m-%d %H:%M"),
                              weekday=now.strftime("%A"), phrase=phrase),
                max_tokens=200, temperature=0,
            )
            data = json.loads(re.search(r"\{.*\}", reply.text, re.S).group(0))
            if not data.get("at"):
                return None
            moment = datetime.strptime(data["at"], "%Y-%m-%d %H:%M").astimezone()
            return Schedule(moment, data.get("repeat"), data.get("text") or phrase)
        except Exception:  # noqa: BLE001 — не понял время, это не авария
            log.info("модель не разобрала время в «%s»", phrase[:80], exc_info=True)
            return None

    @staticmethod
    def _strip_time_words(phrase: str) -> str:
        """Убрать из текста саму временную часть, оставив суть."""
        cleaned = re.sub(
            r"(через\s+\d+\s*[а-яё]+|кажд\w+\s+день|ежедневно|завтра|сегодня|послезавтра)"
            r"\s*(в\s*\d{1,2}(:\d{2})?)?",
            "", phrase, flags=re.I,
        )
        return " ".join(cleaned.split()).strip(" ,.—-") or phrase
