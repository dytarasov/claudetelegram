"""Общее для репозиториев.

Репозиторий здесь — единственное место, где живёт SQL. Наверх он отдаёт
доменные модели, а не asyncpg.Record: сервис не должен знать ни имён колонок,
ни того, что jsonb приезжает строкой.
"""
from __future__ import annotations

import json
from typing import Any

from ..db.database import Database


def vector_literal(values: list[float]) -> str:
    """Вектор в виде «[0.1,0.2,…]».

    asyncpg не знает типа vector из расширения pgvector, поэтому передаём его
    текстом и приводим в запросе через ::vector. Регистрировать кодек было бы
    красивее, но потребовало бы знать размерность на момент подключения — а она
    задаётся настройкой и может смениться.
    """
    return "[" + ",".join(f"{v:.7g}" for v in values) + "]"


class BaseRepository:
    """Держит ссылку на пул и мелкие помощники разбора."""

    def __init__(self, db: Database) -> None:
        self._db = db

    @staticmethod
    def _json_list(raw: Any) -> list:
        """jsonb-колонка приезжает строкой — asyncpg не декодирует её сам."""
        if raw is None:
            return []
        if isinstance(raw, (list, tuple)):
            return list(raw)
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return value if isinstance(value, list) else []


def or_terms(query: str) -> str:
    """Переписать запрос так, чтобы хватало совпадения по части слов.

    websearch_to_tsquery по умолчанию соединяет термины через AND, и вопрос
    «чего нельзя трогать на сервере» не находит запись «shadowtunnel не
    трогать» — мешает лишнее «чего». Для коротких выверенных записей (факты,
    события) полнота важнее точности: пусть найдётся лишнее, чем не найдётся
    нужное, ранжирование поднимет наверх совпавшее по большему числу слов.

    Синтаксис OR понимает сам websearch_to_tsquery, поэтому экранировать
    ничего не нужно: эта функция не умеет падать на кривом вводе.
    """
    words = [w for w in query.split() if w]
    return " OR ".join(words) if words else query
