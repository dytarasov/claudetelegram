-- Обязательства: то, что кто-то кому-то обещал, и то, о чём надо напомнить.
--
-- Это первый слой памяти, который работает не на вспоминание, а на действие:
-- по нему бот сам начинает разговор. Поэтому у записи есть не только текст и
-- срок, но и состояние, счётчик срабатываний и уровень настойчивости —
-- напоминание, которое молча повторяется одинаково, человек перестаёт замечать.
CREATE TABLE IF NOT EXISTS reminders (
    id             bigserial PRIMARY KEY,
    chat_id        bigint      NOT NULL,
    text           text        NOT NULL,
    kind           text        NOT NULL DEFAULT 'reminder',
    -- reminder   — просто напомнить в момент времени
    -- commitment — обязательство с дедлайном, за него бот пинает настойчиво
    -- followup   — «мы это не доделали», бот заводит сам
    -- digest     — регулярная сводка
    status         text        NOT NULL DEFAULT 'open',
    -- open | done | snoozed | dropped
    due_at         timestamptz NOT NULL,
    repeat_rule    text,          -- daily | weekly:mon | NULL
    urgency        integer     NOT NULL DEFAULT 1,   -- растёт при игнорировании
    fired_count    integer     NOT NULL DEFAULT 0,
    last_fired_at  timestamptz,
    source         text        NOT NULL DEFAULT 'user',   -- user | assistant
    source_turn_id bigint,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    done_at        timestamptz
);

-- Главный запрос планировщика: «что уже пора». Частичный индекс, потому что
-- закрытые напоминания его не интересуют вовсе, а их со временем станет много.
CREATE INDEX IF NOT EXISTS reminders_due_idx ON reminders (due_at)
    WHERE status IN ('open', 'snoozed');
CREATE INDEX IF NOT EXISTS reminders_chat_idx ON reminders (chat_id, status);
