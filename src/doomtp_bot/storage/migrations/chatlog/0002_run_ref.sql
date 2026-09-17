-- Link command runs to what they sent. `run_ref` is the runtime's run id (hex string);
-- the old INTEGER run_id was never written.
ALTER TABLE command_runs ADD COLUMN run_ref TEXT;
ALTER TABLE outbound_msgs DROP COLUMN run_id;
ALTER TABLE outbound_msgs ADD COLUMN run_ref TEXT;
CREATE INDEX ix_command_runs_run_ref ON command_runs(run_ref);
CREATE INDEX ix_outbound_run_ref ON outbound_msgs(run_ref);
