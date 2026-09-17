-- chatlog.db: append-only chat log, moderation events, coverage, usage logs (architecture §3, §4.5)
-- Rows are never deleted in response to moderation; flags and mod_events are added instead.
-- Timestamps are INTEGER milliseconds since the Unix epoch. Users are keyed by user_id.

CREATE TABLE users (
    user_id      TEXT PRIMARY KEY,
    login        TEXT NOT NULL,
    display_name TEXT,
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL
);

CREATE TABLE user_names (
    user_id      TEXT NOT NULL,
    login        TEXT NOT NULL,
    display_name TEXT,
    seen_from    INTEGER NOT NULL,
    PRIMARY KEY (user_id, login)
);

CREATE TABLE messages (
    message_id        TEXT PRIMARY KEY,      -- Twitch UUID (same for EventSub and IRC backfill)
    channel_id        TEXT NOT NULL,
    user_id           TEXT NOT NULL,
    user_login        TEXT NOT NULL,
    display_name      TEXT,
    text              TEXT NOT NULL,
    message_type      TEXT,
    badges            TEXT,                  -- JSON
    fragments         TEXT,                  -- JSON
    bits              INTEGER NOT NULL DEFAULT 0,
    reply_parent_id   TEXT,
    reward_id         TEXT,
    source_channel_id TEXT,
    is_self           INTEGER NOT NULL DEFAULT 0,
    is_command        INTEGER NOT NULL DEFAULT 0,
    source            TEXT NOT NULL DEFAULT 'eventsub' CHECK (source IN ('eventsub', 'recent-messages')),
    raw               TEXT,
    sent_at           INTEGER NOT NULL,
    received_at       INTEGER NOT NULL,
    deleted_at        INTEGER,
    cleared_at        INTEGER,
    mod_event_id      INTEGER
);
CREATE INDEX ix_messages_channel_time ON messages(channel_id, sent_at);
CREATE INDEX ix_messages_user_time ON messages(user_id, sent_at);

CREATE VIRTUAL TABLE messages_fts USING fts5(
    text,
    content = 'messages',
    content_rowid = 'rowid',
    tokenize = 'unicode61 remove_diacritics 2'
);

-- Keep FTS in sync. Text is never edited, but deletes are possible via an explicit admin purge.
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER messages_au AFTER UPDATE OF text ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TABLE chat_notifications (
    id         TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    user_id    TEXT,
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL,                -- JSON
    source     TEXT NOT NULL DEFAULT 'eventsub',
    sent_at    INTEGER NOT NULL
);
CREATE INDEX ix_notifications_channel_time ON chat_notifications(channel_id, sent_at);

CREATE TABLE mod_events (
    id                INTEGER PRIMARY KEY,
    channel_id        TEXT NOT NULL,
    type              TEXT NOT NULL CHECK (type IN ('delete', 'user_clear', 'chat_clear', 'timeout', 'ban', 'unban')),
    message_id        TEXT,
    target_user_id    TEXT,
    moderator_user_id TEXT,
    duration_s        INTEGER,
    reason            TEXT,
    source            TEXT NOT NULL DEFAULT 'eventsub',
    at                INTEGER NOT NULL
);
CREATE INDEX ix_mod_events_channel_time ON mod_events(channel_id, at);

CREATE TABLE log_sessions (
    id         INTEGER PRIMARY KEY,
    channel_id TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    ended_at   INTEGER,
    end_reason TEXT                           -- shutdown | update | crash | reconnect | part
);
CREATE INDEX ix_log_sessions_channel ON log_sessions(channel_id, started_at);

CREATE TABLE backfill_runs (
    id         INTEGER PRIMARY KEY,
    channel_id TEXT NOT NULL,
    gap_from   INTEGER NOT NULL,
    gap_to     INTEGER NOT NULL,
    fetched    INTEGER NOT NULL DEFAULT 0,
    inserted   INTEGER NOT NULL DEFAULT 0,
    complete   INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    at         INTEGER NOT NULL
);

CREATE TABLE command_runs (
    id               INTEGER PRIMARY KEY,
    channel_id       TEXT NOT NULL,
    user_id          TEXT,
    trigger_type     TEXT NOT NULL,          -- chat | timer | redemption | listener | api | ...
    trigger_id       TEXT,
    expr             TEXT NOT NULL,
    resolved         TEXT,                   -- JSON: resolution per invocation
    code             INTEGER NOT NULL,
    message          TEXT,
    duration_ms      INTEGER NOT NULL,
    cancelled_reason TEXT,
    at               INTEGER NOT NULL
);
CREATE INDEX ix_command_runs_channel_time ON command_runs(channel_id, at);

CREATE TABLE outbound_msgs (
    id                INTEGER PRIMARY KEY,
    channel_id        TEXT NOT NULL,
    run_id            INTEGER,
    text_sent         TEXT,
    text_prefilter    TEXT,
    filter_hits       TEXT,                   -- JSON
    twitch_message_id TEXT,
    dropped_reason    TEXT,                   -- moderated | ttl | rate_limited | twitch_rejected | filter_block
    at                INTEGER NOT NULL
);
CREATE INDEX ix_outbound_channel_time ON outbound_msgs(channel_id, at);
