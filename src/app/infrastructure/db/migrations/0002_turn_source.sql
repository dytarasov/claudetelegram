-- Откуда пришёл ход: напечатан, наговорен или прислан файлом.
-- Совместимо со старым кодом: колонка с DEFAULT, ничего не переименовано —
-- версия до этой миграции продолжает писать в turns как ни в чём не бывало.
ALTER TABLE turns ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'text';
CREATE INDEX IF NOT EXISTS turns_source_idx ON turns (source);
