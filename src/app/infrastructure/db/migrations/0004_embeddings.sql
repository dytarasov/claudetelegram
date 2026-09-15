-- Семантический поиск по журналу и заметкам.
--
-- Колонки embedding появились ещё в 0001 — тогда пустые, «на будущее». Теперь
-- добавляем то, без чего ими нельзя пользоваться всерьёз:
--
--   embedding_model — какой моделью посчитан вектор. Без этого смена модели
--                     эмбеддингов означала бы молча смешанный индекс, где
--                     половина векторов из одного пространства, половина из
--                     другого, и расстояния между ними бессмысленны.
--   индекс hnsw     — иначе поиск по вектору это последовательный проход по
--                     всей таблице. На сотне записей незаметно, на сотне тысяч
--                     смертельно.
--
-- Косинусная мера (vector_cosine_ops) выбрана потому, что все распространённые
-- модели эмбеддингов отдают нормализованные векторы, и для них это стандарт.
ALTER TABLE turns ADD COLUMN IF NOT EXISTS embedding_model text;
ALTER TABLE notes ADD COLUMN IF NOT EXISTS embedding_model text;

CREATE INDEX IF NOT EXISTS turns_embedding_idx ON turns USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS notes_embedding_idx ON notes USING hnsw (embedding vector_cosine_ops);

-- Частый запрос фоновой индексации: «дай записи без вектора».
CREATE INDEX IF NOT EXISTS turns_unindexed_idx ON turns (id) WHERE embedding IS NULL;
CREATE INDEX IF NOT EXISTS notes_unindexed_idx ON notes (id) WHERE embedding IS NULL;
