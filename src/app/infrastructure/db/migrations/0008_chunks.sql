-- Куски: единица поиска по памяти.
--
-- Раньше вектор считался один на весь ход целиком — вопрос и ответ склеивались
-- в одну строку и сжимались в одну точку. Две беды сразу.
--
-- Первая: размытие. Длинный ответ, где разобраны и миграции, и кнопки, и
-- распознавание речи, превращается в усреднённую точку, не похожую толком ни на
-- один из своих кусков. Найти по такому вектору конкретную мысль нельзя.
--
-- Вторая: обрезка. В индекс уходили первые 2000 символов вопроса и 4000 ответа,
-- остальное просто не существовало для поиска. На сороковом ходу обрезанными
-- были уже три записи.
--
-- Отсюда отдельная таблица. Один кусок — одна законченная мысль на 900-1400
-- символов, со своим вектором и своим полнотекстовым индексом. Реплика человека
-- и мой ответ режутся раздельно: вопрос «почему пусто?» и ответ на три экрана
-- имеют мало общего, и усреднять их — терять оба.
CREATE TABLE IF NOT EXISTS chunks (
    id          bigserial PRIMARY KEY,
    source_kind text        NOT NULL,          -- turn | note
    source_id   bigint      NOT NULL,
    ord         integer     NOT NULL,          -- порядок внутри записи
    role        text        NOT NULL,          -- user | assistant
    text        text        NOT NULL,
    created_at  timestamptz NOT NULL,
    -- Время исходной записи, а не вставки куска. Так поиск умеет сортировать и
    -- ограничивать по времени без соединения с turns, а перестроение индекса не
    -- делает вид, что весь разговор случился сегодня.
    embedding       vector(1024),
    embedding_model text,
    search      tsvector GENERATED ALWAYS AS (to_tsvector('russian', text)) STORED,

    -- Тождество куска. Перестроение нарезки для записи идёт как «удалить всё её
    -- и вставить заново», и уникальность защищает от половинчатого результата,
    -- когда старые куски остались рядом с новыми.
    UNIQUE (source_kind, source_id, ord)
);

CREATE INDEX IF NOT EXISTS chunks_source_idx    ON chunks (source_kind, source_id);
CREATE INDEX IF NOT EXISTS chunks_search_idx    ON chunks USING gin (search);
CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_unindexed_idx ON chunks (id) WHERE embedding IS NULL;
CREATE INDEX IF NOT EXISTS chunks_when_idx      ON chunks (created_at DESC);

-- Колонки turns.embedding и notes.embedding сознательно остаются на месте, хотя
-- поиск ими больше не пользуется. Причина в откате: сторож умеет вернуть код на
-- предыдущую версию, а миграции назад не отматываются. Уронив колонку сейчас, я
-- получу версию, которая при откате начнёт падать на каждом запросе к журналу.
-- Уберутся они отдельной миграцией, когда эта поживёт стабильной.
