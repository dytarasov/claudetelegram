-- Первая схема. Правило миграций: только вперёд и только совместимо.
-- Откат кода (git reset сторожем) не откатывает базу, поэтому новая миграция не
-- имеет права ломать старый код: колонки добавляем с default, ничего не
-- переименовываем и не удаляем, пока живёт версия, которая на это смотрит.

CREATE TABLE IF NOT EXISTS schema_migrations (
    name       text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

-- История самодопила: зачем катили, чем кончилось, сколько поднимались.
CREATE TABLE IF NOT EXISTS deployments (
    id           bigserial PRIMARY KEY,
    note         text        NOT NULL,
    prev_sha     text        NOT NULL,
    new_sha      text        NOT NULL,
    phase        text        NOT NULL,
    trigger      text        NOT NULL DEFAULT 'assistant',
    files        jsonb       NOT NULL DEFAULT '[]'::jsonb,
    reason       text,
    chat_id      bigint,
    started_at   timestamptz,
    finished_at  timestamptz,
    revived_in_s double precision,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS deployments_new_sha_idx ON deployments (new_sha);
CREATE INDEX IF NOT EXISTS deployments_created_idx ON deployments (created_at DESC);

-- Журнал ходов: моя память о том, что тут происходило и во что обошлось.
-- search — генерируемый tsvector: поиск работает сразу, без внешних сервисов.
-- embedding — под будущую семантику; пока NULL, но колонка и тип уже на месте,
-- чтобы включение эмбеддингов не требовало переливки данных.
CREATE TABLE IF NOT EXISTS turns (
    id         bigserial PRIMARY KEY,
    chat_id    bigint      NOT NULL,
    user_id    bigint,
    prompt     text        NOT NULL,
    answer     text        NOT NULL DEFAULT '',
    status     text        NOT NULL DEFAULT 'ok',
    tools      jsonb       NOT NULL DEFAULT '[]'::jsonb,
    session_id text,
    tokens_in  integer     NOT NULL DEFAULT 0,
    tokens_out integer     NOT NULL DEFAULT 0,
    cost_usd   double precision NOT NULL DEFAULT 0,
    duration_s double precision NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    search     tsvector GENERATED ALWAYS AS (
                   to_tsvector('russian', coalesce(prompt, '') || ' ' || coalesce(answer, ''))
               ) STORED,
    embedding  vector(1536)
);
CREATE INDEX IF NOT EXISTS turns_search_idx  ON turns USING gin (search);
CREATE INDEX IF NOT EXISTS turns_created_idx ON turns (created_at DESC);

-- Заметки себе: выводы, договорённости, грабли. Ход — факт, заметка — вывод.
CREATE TABLE IF NOT EXISTS notes (
    id         bigserial PRIMARY KEY,
    text       text        NOT NULL,
    tags       text[]      NOT NULL DEFAULT '{}',
    source     text        NOT NULL DEFAULT 'assistant',
    created_at timestamptz NOT NULL DEFAULT now(),
    search     tsvector GENERATED ALWAYS AS (to_tsvector('russian', text)) STORED,
    embedding  vector(1536)
);
CREATE INDEX IF NOT EXISTS notes_search_idx ON notes USING gin (search);
CREATE INDEX IF NOT EXISTS notes_trgm_idx   ON notes USING gin (text gin_trgm_ops);

-- Мелкое состояние между перезапусками (бывший state.json).
CREATE TABLE IF NOT EXISTS kv (
    key        text PRIMARY KEY,
    value      text        NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
