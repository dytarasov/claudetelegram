"""Векторизация текста через OpenAI-совместимый /embeddings.

Совместимый формат выбран сознательно: под него подходят OpenRouter (baai/bge-m3
и другие), OpenAI, Voyage, Jina, DeepInfra и локальные серверы. Провайдер
задаётся адресом и ключом в .env, а код остаётся один.

Важное про размерность. Колонка в БД объявлена как vector(N), и N обязан
совпадать с длиной вектора модели: 1536 у text-embedding-3-small, 1024 у многих
других, 3072 у больших. Несовпадение база поймает на первой же записи, поэтому
клиент проверяет длину сам и говорит об этом внятно, а не отдаёт ошибку SQL.
Сменить размерность в базе умеет `cli.py embeddings resize`.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence

from ...settings import Settings
from .base import LLMError, post_json

log = logging.getLogger(__name__)

# Больше за раз отправлять смысла нет: провайдеры ограничивают размер запроса,
# а батч всё равно упирается в сеть, а не в их скорость.
BATCH = 64


class OpenAICompatibleEmbedder:
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    def available(self) -> bool:
        return bool(self._s.embeddings_api_key and self._s.embeddings_model)

    def dimensions(self) -> int:
        return self._s.embeddings_dim

    def model_name(self) -> str:
        return self._s.embeddings_model

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not self.available():
            raise LLMError("ключ к модели эмбеддингов не задан (EMBEDDINGS_API_KEY)")
        clean = [t.strip()[: self._s.embeddings_max_chars] or " " for t in texts]

        vectors: list[list[float]] = []
        for start in range(0, len(clean), BATCH):
            chunk = clean[start:start + BATCH]
            payload: dict = {"model": self._s.embeddings_model, "input": chunk}
            # Часть провайдеров умеет резать вектор на своей стороне — это дешевле,
            # чем хранить лишние измерения, но поддерживают не все, поэтому опция.
            if self._s.embeddings_send_dimensions:
                payload["dimensions"] = self._s.embeddings_dim
            data = await post_json(f"{self._s.embeddings_base_url}/embeddings", payload,
                                   {"Authorization": f"Bearer {self._s.embeddings_api_key}",
                                    "Content-Type": "application/json"})
            try:
                # Порядок в ответе не гарантирован — раскладываем по index.
                rows = sorted(data["data"], key=lambda item: item.get("index", 0))
                vectors.extend([float(x) for x in row["embedding"]] for row in rows)
            except (KeyError, TypeError, ValueError) as exc:
                raise LLMError(f"неожиданный ответ эмбеддера: {str(data)[:200]}") from exc

        if vectors and len(vectors[0]) != self._s.embeddings_dim:
            raise LLMError(
                f"модель вернула вектор длины {len(vectors[0])}, а в базе колонка "
                f"vector({self._s.embeddings_dim}). Поменяй EMBEDDINGS_DIM и выполни "
                f"`cli.py embeddings resize {len(vectors[0])}`"
            )
        return vectors
