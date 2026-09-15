"""Термины, которые модель распознавания слышит неправильно.

Почему это вообще нужно. Штатный способ подсказать виспер-у редкие слова —
initial_prompt или hotwords. С русским файнтюном large-v3-turbo он не работает:
от любой подсказки модель возвращает пустую строку. Поэтому термины чиним после
распознавания.

Почему словарь не зашит намертво. Здесь лежит только затравка — то, с чего
начинается пустая база. Живой словарь хранится в таблице stt_terms и
пополняется на ходу: увидел в расшифровке новое кривое слово — добавил, и оно
чинится со следующего же голосового, без правки кода и без перезапуска.

В этом файле нет ввода-вывода: чистые данные и чистая функция замены, чтобы всё
это можно было проверить тестами и переиспользовать где угодно.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Затравка: канонический вид → как его слышит модель.
SEED_TERMS: dict[str, tuple[str, ...]] = {
    "Claude Code": ("клауди код", "клаудикод", "клауд код", "клаудкод", "клод код",
                    "клодкод", "клауди-код", "клод-код", "кладкод", "клауд-код"),
    "Claude": ("клауди", "клод", "клауд"),
    "Python": ("пайтон", "питон", "пайтона", "питона"),
    "AI": ("яй", "эй ай", "а и"),
    "LLM": ("лм", "эл эм", "элэм", "ллм"),
    "API": ("апи", "эй пи ай"),
    "git": ("гит",),
    "GitHub": ("гитхаб", "гит хаб"),
    "Telegram": ("телеграм", "телеграмм"),
    "systemd": ("систем ди", "системд", "система ди"),
    "Docker": ("докер", "доккер"),
    "nginx": ("энджинкс", "нгинкс", "энжинкс"),
    "PostgreSQL": ("постгрес", "постгресс", "постгре"),
    "pgvector": ("пиджи вектор", "пг вектор", "пеговектор", "пего вектор",
                 "пеквектор", "пидживектор", "пг-вектор"),
    "dishka": ("дишка", "дишку", "дышка", "дишки", "дишкой"),
    "aiogram": ("айограм", "аиограм"),
    "Whisper": ("виспер", "вишпер", "whisper"),
    "Opus": ("опус",),
    "Sonnet": ("соннет", "сонет"),
    "DTO": ("дэ тэ о", "дито"),
    "DI": ("ди ай",),
    "fail2ban": ("фейл ту бан", "фейлтубан", "фейл2бан"),
    "эмбеддинг": ("эмбейдинг", "эмбединг", "эмбидинг", "имбединг", "имбеддинг"),
    "эмбеддинги": ("эмбейдинги", "эмбединги", "эмбидинги"),
    "эмбеддингов": ("эмбейдингов", "эмбедингов"),
}


@dataclass(frozen=True, slots=True)
class Replacement:
    """Что на что заменили — уходит и в лог, и модели вместе с расшифровкой."""

    heard: str
    canonical: str

    def __str__(self) -> str:
        return f"{self.heard} → {self.canonical}"


class TermIndex:
    """Скомпилированный словарь: один регэксп на все варианты сразу.

    Длинные варианты идут первыми, поэтому «клауди код» выигрывает у «клауди» и
    не оставляет осиротевшего «код». Границы слова проверяются, чтобы «гит» не
    портил «гитару», а «питон» — «питончика».
    """

    def __init__(self, mapping: dict[str, tuple[str, ...]] | dict[str, list[str]]) -> None:
        pairs = sorted(
            ((v, canon) for canon, variants in mapping.items() for v in variants),
            key=lambda pair: -len(pair[0]),
        )
        self._lookup = {variant.lower(): canon for variant, canon in pairs}
        self._pattern = (
            re.compile(r"(?<![\w-])(" + "|".join(re.escape(v) for v, _ in pairs) + r")(?![\w-])",
                       re.IGNORECASE)
            if pairs else None
        )

    def apply(self, text: str) -> tuple[str, list[Replacement]]:
        if not text or self._pattern is None:
            return text, []
        applied: list[Replacement] = []

        def swap(match: re.Match[str]) -> str:
            canonical = self._lookup[match.group(0).lower()]
            applied.append(Replacement(match.group(0), canonical))
            return canonical

        return self._pattern.sub(swap, text), applied
