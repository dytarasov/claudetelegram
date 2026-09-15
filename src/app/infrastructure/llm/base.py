"""Общее для клиентов внешних моделей: HTTP, повторы, ошибки.

Один запрос к чужому API — это всегда три вопроса: как аутентифицируемся, что
делать с 429 и что делать с обрывом сети. Ответы одинаковые для чат-модели и
для эмбеддера, поэтому лежат здесь, а не дублируются в обоих клиентах.

Повторяем только то, что имеет смысл повторять: перегрузку (429) и серверные
ошибки (5xx). На 400 и 401 повтор бессмысленен — там виноват запрос или ключ,
и молча долбиться в закрытую дверь значит прятать причину.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from ...domain.errors import AppError

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
ATTEMPTS = 3
BACKOFF_BASE = 1.5


class LLMError(AppError):
    """Внешняя модель не ответила или ответила не так, как обещала."""


async def post_json(url: str, payload: dict, headers: dict[str, str],
                    timeout: float = 120.0) -> dict[str, Any]:
    """POST с ретраями. Возвращает разобранный JSON или кидает LLMError."""
    last: str = "неизвестно"
    for attempt in range(1, ATTEMPTS + 1):
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    body = await resp.text()
                    if resp.status in RETRY_STATUSES:
                        last = f"{resp.status}: {body[:200]}"
                        # Провайдер может сам сказать, сколько ждать.
                        pause = float(resp.headers.get("Retry-After") or BACKOFF_BASE ** attempt)
                        log.warning("%s ответил %s, повтор через %.1fс (%d/%d)",
                                    url, resp.status, pause, attempt, ATTEMPTS)
                        await asyncio.sleep(min(pause, 30))
                        continue
                    if resp.status >= 400:
                        raise LLMError(f"{url} вернул {resp.status}: {body[:300]}")
                    try:
                        return await resp.json()
                    except (aiohttp.ContentTypeError, ValueError) as exc:
                        raise LLMError(f"{url} вернул не JSON: {body[:200]}") from exc
        except asyncio.TimeoutError:
            last = f"нет ответа за {timeout:.0f}с"
            log.warning("%s: таймаут, повтор (%d/%d)", url, attempt, ATTEMPTS)
        except aiohttp.ClientError as exc:
            last = f"сеть: {exc}"
            log.warning("%s: сбой сети (%s), повтор (%d/%d)", url, exc, attempt, ATTEMPTS)
        if attempt < ATTEMPTS:
            await asyncio.sleep(BACKOFF_BASE ** attempt)
    raise LLMError(f"{url} недоступен после {ATTEMPTS} попыток · {last}")
