-- migrate:up
CREATE UNIQUE INDEX IF NOT EXISTS idx_summaries_channel_day_trigger_unique
ON summaries(channel_id, day, trigger);

-- migrate:down
DROP INDEX IF EXISTS idx_summaries_channel_day_trigger_unique;
