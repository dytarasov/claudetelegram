-- Два слоя памяти поверх сырого журнала: факты и события.
--
-- Зачем отдельные таблицы, если все реплики и так лежат в turns. Потому что
-- искать по сырому разговору — это каждый раз заново вычитывать из болтовни
-- то, что уже было понято однажды. «Postgres слушает 5433, потому что 5432
-- занят контейнером VPN» сказано в одной реплике посреди отладки; найти её
-- поиском можно, но дорого и ненадёжно. Факт — это уже сделанный вывод,
-- хранящийся отдельно от разговора, в котором он прозвучал.
--
-- Разделение facts / events — по тому, есть ли у записи время.
--   facts  отвечают на «как есть»: константы, предпочтения, правила. У них нет
--          момента, есть период действия — от valid_from до valid_until.
--   events отвечают на «когда было»: встречи, решения, платежи, сроки. У них
--          есть точка на оси времени, и главный запрос к ним — диапазон дат.
-- Смешивать их в одну таблицу заманчиво, но тогда любой запрос обязан начинаться
-- с «а это запись со временем или без», и индексы получаются бесполезные для
-- обоих случаев сразу.

-- ---------------------------------------------------------------------------
--  Факты
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS facts (
    id             bigserial PRIMARY KEY,
    subject        text        NOT NULL DEFAULT 'user',
    -- о ком/чём факт: user, server, project, или имя человека («тётя Шура»).
    -- Позволяет достать профиль одного субъекта, не перебирая всё подряд.
    key            text,
    -- краткий ключ вроде «таймзона» или «порт postgres». Именно ключ, а не
    -- текст, задаёт тождество факта: запись с тем же subject+key заменяет
    -- прежнюю, а не ложится рядом. Без ключа факт просто добавляется.
    value          text        NOT NULL,
    kind           text        NOT NULL DEFAULT 'fact',
    -- fact       — утверждение о мире («сестре нужна доставка DHL»)
    -- constant   — то, что меняется крайне редко («порт 5433»)
    -- preference — вкус и привычка («не любит эмодзи»)
    -- rule       — запрет или требование («shadowtunnel не трогать»)
    pinned         boolean     NOT NULL DEFAULT false,
    -- входит в профиль, который агент читает перед работой. Пометка ручная и
    -- должна оставаться редкой: профиль ценен ровно до тех пор, пока он
    -- помещается в голову целиком.
    confidence     real        NOT NULL DEFAULT 1.0,
    -- 1.0 — сказано прямо, 0.5 — выведено из контекста. Низкую уверенность
    -- лучше хранить с пометкой, чем не хранить вовсе или выдавать за истину.
    tags           text[]      NOT NULL DEFAULT '{}',
    source         text        NOT NULL DEFAULT 'assistant',   -- user | assistant
    source_turn_id bigint,
    -- откуда факт взялся. Без этого через месяц невозможно отличить то, что
    -- человек сказал, от того, что я сам себе придумал.
    valid_from     timestamptz NOT NULL DEFAULT now(),
    valid_until    timestamptz,
    -- NULL значит «действует сейчас». Факты не удаляются, а закрываются:
    -- «раньше жил в Москве» — это не мусор, это история, и она нужна, чтобы
    -- понимать старые реплики.
    superseded_by  bigint      REFERENCES facts(id) ON DELETE SET NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    search         tsvector GENERATED ALWAYS AS (
        to_tsvector('russian', coalesce(key, '') || ' ' || value)
    ) STORED
);

-- Главный запрос: «действующие факты про такого-то». Частичный индекс, потому
-- что закрытые версии со временем перевесят живые, а спрашивают почти всегда
-- живые.
CREATE INDEX IF NOT EXISTS facts_active_idx ON facts (subject, key)
    WHERE valid_until IS NULL;
CREATE INDEX IF NOT EXISTS facts_pinned_idx ON facts (id)
    WHERE pinned AND valid_until IS NULL;
CREATE INDEX IF NOT EXISTS facts_search_idx ON facts USING gin (search);
CREATE INDEX IF NOT EXISTS facts_trgm_idx   ON facts USING gin (value gin_trgm_ops);

-- ---------------------------------------------------------------------------
--  События
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id             bigserial PRIMARY KEY,
    title          text        NOT NULL,
    details        text,
    happened_at    timestamptz NOT NULL,
    -- одна точка на оси, а не диапазон: так проще индексировать и сортировать.
    -- Событие длиной в неделю пишется как два — начало и конец.
    date_precision text        NOT NULL DEFAULT 'minute',
    -- minute | day | month | year. «Где-то в марте» и «в 15:00» одинаково
    -- законные воспоминания, но выводить их надо по-разному, иначе получается
    -- ложная точность: «1 марта 00:00» там, где человек сказал «в марте».
    kind           text        NOT NULL DEFAULT 'event',
    -- event | decision | deadline | meeting | payment | trip
    people         text[]      NOT NULL DEFAULT '{}',
    tags           text[]      NOT NULL DEFAULT '{}',
    source         text        NOT NULL DEFAULT 'assistant',
    source_turn_id bigint,
    created_at     timestamptz NOT NULL DEFAULT now(),
    search         tsvector GENERATED ALWAYS AS (
        to_tsvector('russian', title || ' ' || coalesce(details, ''))
    ) STORED
);

-- Основной способ спросить события — диапазон дат, поэтому индекс по времени.
CREATE INDEX IF NOT EXISTS events_when_idx   ON events (happened_at DESC);
CREATE INDEX IF NOT EXISTS events_search_idx ON events USING gin (search);
CREATE INDEX IF NOT EXISTS events_trgm_idx   ON events USING gin (title gin_trgm_ops);

-- Про векторы здесь сознательно ничего нет. Факт и событие — это одна короткая
-- строка с ключом и тегами; полнотекст с триграммами находит их надёжно, а
-- закреплённые факты вообще отдаются целиком, без всякого поиска. Семантика
-- нужна там, где надо искать по смыслу в длинной сырой речи, — то есть в turns,
-- где она и включена.
