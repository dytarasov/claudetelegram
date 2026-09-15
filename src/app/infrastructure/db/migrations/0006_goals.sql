-- Цели: то, к чему бот идёт сам, без реплики человека.
--
-- Почему в базе, а не в планировщике харнесса. У Claude Code есть свои /loop и
-- cron, но они живут в памяти сессии, а наш бот перезапускает процесс claude
-- при каждой самовыкатке — за одну ночь это десяток раз. Расписание, которое
-- испаряется на деплое, не расписание. Здесь оно переживает и перезапуск, и
-- откат, и падение.
CREATE TABLE IF NOT EXISTS goals (
    id             bigserial PRIMARY KEY,
    chat_id        bigint      NOT NULL,
    text           text        NOT NULL,   -- что сделать
    done_when      text        NOT NULL,   -- по какому признаку считать законченным
    status         text        NOT NULL DEFAULT 'active',  -- active|paused|done|failed|stopped
    cadence_min    integer     NOT NULL DEFAULT 60,        -- как часто просыпаться
    -- Ограничители. Без них автономный агент — это счётчик, который крутится,
    -- пока человек спит: и деньги, и риск растут молча.
    budget_usd     double precision NOT NULL DEFAULT 3.0,
    spent_usd      double precision NOT NULL DEFAULT 0,
    max_turns_day  integer     NOT NULL DEFAULT 8,
    turns_today    integer     NOT NULL DEFAULT 0,
    turns_day      date,
    next_run_at    timestamptz NOT NULL DEFAULT now(),
    last_run_at    timestamptz,
    steps_done     integer     NOT NULL DEFAULT 0,
    last_note      text,                    -- чем закончился прошлый заход
    created_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz
);
CREATE INDEX IF NOT EXISTS goals_due_idx ON goals (next_run_at) WHERE status = 'active';

-- Журнал продвижения: без него бот на каждом пробуждении начинает с нуля и
-- ходит по кругу, считая это работой.
CREATE TABLE IF NOT EXISTS goal_steps (
    id         bigserial PRIMARY KEY,
    goal_id    bigint      NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    summary    text        NOT NULL,
    cost_usd   double precision NOT NULL DEFAULT 0,
    turn_id    bigint,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS goal_steps_goal_idx ON goal_steps (goal_id, id DESC);

-- Чтобы можно было связать расход хода с целью, ради которой он был сделан.
ALTER TABLE turns ADD COLUMN IF NOT EXISTS goal_id bigint;
