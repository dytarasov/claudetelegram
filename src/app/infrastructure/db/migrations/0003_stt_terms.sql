-- Живой словарь терминов для расшифровки голосовых.
-- Затравка (domain/terms.py) остаётся в коде, а всё, что добавлено на ходу,
-- живёт здесь: пополнение словаря не должно требовать правки кода и выкатки.
CREATE TABLE IF NOT EXISTS stt_terms (
    id         bigserial PRIMARY KEY,
    canonical  text        NOT NULL,
    variant    text        NOT NULL,
    source     text        NOT NULL DEFAULT 'assistant',
    created_at timestamptz NOT NULL DEFAULT now()
);
-- Один и тот же услышанный вариант не может вести к двум разным терминам.
CREATE UNIQUE INDEX IF NOT EXISTS stt_terms_variant_idx ON stt_terms (lower(variant));
