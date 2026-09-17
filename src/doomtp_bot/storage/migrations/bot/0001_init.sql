-- bot.db: state and configuration (architecture §5, §6, §7, §9, §10; ADR-0006, 0007, 0009, 0010)
-- Conventions:
--   * timestamps are INTEGER milliseconds since the Unix epoch
--   * users are keyed by Twitch user_id, never login
--   * global (non-channel) rows use channel_id = '*' (NULLs don't dedupe in SQLite PKs)

-- ── Auth ─────────────────────────────────────────────────────────────────────
CREATE TABLE oauth_tokens (
    identity      TEXT PRIMARY KEY,          -- 'bot' | 'broadcaster:<user_id>'
    user_id       TEXT NOT NULL,
    login         TEXT NOT NULL,
    access_token  TEXT NOT NULL,
    refresh_token TEXT,
    scopes        TEXT NOT NULL DEFAULT '[]', -- JSON array
    expires_at    INTEGER,
    updated_at    INTEGER NOT NULL
);

CREATE TABLE api_keys (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,       -- argon2/sha256 of the key; the key itself is never stored
    owner_user_id TEXT,
    scopes       TEXT NOT NULL DEFAULT '[]',
    created_at   INTEGER NOT NULL,
    last_used_at INTEGER,
    revoked_at   INTEGER
);

-- ── Channels (ADR-0007) ─────────────────────────────────────────────────────
CREATE TABLE channels (
    channel_id             TEXT PRIMARY KEY,
    login                  TEXT NOT NULL,
    active                 INTEGER NOT NULL DEFAULT 1,
    status                 TEXT NOT NULL DEFAULT 'joined',  -- joined | parted | banned
    tier                   TEXT NOT NULL DEFAULT 'basic',   -- basic | moderator | full
    capabilities           TEXT NOT NULL DEFAULT '[]',      -- JSON array
    prefix                 TEXT NOT NULL DEFAULT '!',
    reply_hold_ms          INTEGER NOT NULL DEFAULT 0,
    log_enabled            INTEGER NOT NULL DEFAULT 1,
    history_backfill       INTEGER NOT NULL DEFAULT 0,      -- opt-in (ADR-0008)
    quiet_errors           INTEGER NOT NULL DEFAULT 0,
    cc_edit_notice         INTEGER NOT NULL DEFAULT 0,
    timezone               TEXT NOT NULL DEFAULT 'UTC',
    channel_var_write_role TEXT NOT NULL DEFAULT 'moderator',
    grant_min_role         TEXT NOT NULL DEFAULT 'moderator',
    publish_min_role       TEXT NOT NULL DEFAULT 'moderator',
    create_min_role        TEXT NOT NULL DEFAULT 'everyone',
    var_admin_role         TEXT NOT NULL DEFAULT 'moderator',
    joined_by              TEXT,
    added_at               INTEGER NOT NULL,
    updated_at             INTEGER NOT NULL
);

-- ── Roles (ADR-0006) ────────────────────────────────────────────────────────
CREATE TABLE roles (
    id         INTEGER PRIMARY KEY,
    channel_id TEXT NOT NULL,                -- '*' = global
    name       TEXT NOT NULL,
    rank       INTEGER NOT NULL CHECK (rank BETWEEN 0 AND 10000),
    builtin    INTEGER NOT NULL DEFAULT 0,
    created_by TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE (channel_id, name)
);

CREATE TABLE role_members (
    role_id    INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL,
    granted_by TEXT,
    granted_at INTEGER NOT NULL,
    expires_at INTEGER,
    PRIMARY KEY (role_id, user_id)
);
CREATE INDEX ix_role_members_user ON role_members(user_id);

CREATE TABLE global_admins (                 -- bot_admin (rank 1000); bot_owner comes from config
    user_id    TEXT PRIMARY KEY,
    granted_by TEXT NOT NULL,
    granted_at INTEGER NOT NULL
);

-- ── Policy: toggles, permissions, cooldowns, callbacks, ignore ──────────────
CREATE TABLE module_toggles (
    channel_id TEXT NOT NULL,                -- '*' = global
    module     TEXT NOT NULL,
    enabled    INTEGER NOT NULL,
    PRIMARY KEY (channel_id, module)
);

CREATE TABLE command_toggles (
    channel_id TEXT NOT NULL,
    command    TEXT NOT NULL,
    enabled    INTEGER,                      -- NULL = inherit
    log_level  TEXT,                         -- NULL = spec default; off|errors|output|invocations|all
    PRIMARY KEY (channel_id, command)
);

CREATE TABLE command_rules (
    channel_id    TEXT NOT NULL,
    command       TEXT NOT NULL,
    required_role TEXT,
    allowed_roles TEXT,                      -- JSON array
    PRIMARY KEY (channel_id, command)
);

CREATE TABLE cooldown_rules (
    channel_id TEXT NOT NULL,
    command    TEXT NOT NULL,
    role       TEXT NOT NULL,
    tier_s     INTEGER NOT NULL CHECK (tier_s >= 0),
    user_s     INTEGER NOT NULL CHECK (user_s >= 0),
    PRIMARY KEY (channel_id, command, role)
);

CREATE TABLE callbacks (
    channel_id TEXT NOT NULL,
    scope      TEXT NOT NULL,                -- 'channel' | 'module:<name>' | 'command:<name>'
    kind       TEXT NOT NULL CHECK (kind IN ('on_cooldown', 'on_denied')),
    expr       TEXT NOT NULL,
    syntax_version TEXT NOT NULL,
    updated_by TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (channel_id, scope, kind)
);

CREATE TABLE ignore_list (
    channel_id TEXT NOT NULL,                -- '*' = global
    user_id    TEXT NOT NULL,
    reason     TEXT,
    added_by   TEXT,
    added_at   INTEGER NOT NULL,
    PRIMARY KEY (channel_id, user_id)
);

-- ── Custom commands (ADR-0009) ──────────────────────────────────────────────
CREATE TABLE custom_commands (
    id              TEXT PRIMARY KEY,        -- e.g. cc_7f3k2
    owner_user_id   TEXT NOT NULL,
    name            TEXT NOT NULL,
    summary         TEXT,
    description     TEXT,
    params          TEXT NOT NULL DEFAULT '[]',
    examples        TEXT NOT NULL DEFAULT '[]',
    data_schema     TEXT,
    current_version INTEGER NOT NULL,
    visibility      TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'shareable')),
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted', 'banned')),
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    UNIQUE (owner_user_id, name)
);

CREATE TABLE custom_command_versions (
    command_id     TEXT NOT NULL REFERENCES custom_commands(id) ON DELETE CASCADE,
    version        INTEGER NOT NULL,
    body           TEXT NOT NULL,
    params         TEXT NOT NULL DEFAULT '[]',
    syntax_version TEXT NOT NULL,
    created_at     INTEGER NOT NULL,
    PRIMARY KEY (command_id, version)
);

CREATE TABLE custom_command_links (
    user_id    TEXT NOT NULL,
    alias      TEXT NOT NULL,
    command_id TEXT NOT NULL REFERENCES custom_commands(id),
    created_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, alias)
);

CREATE TABLE custom_command_publications (
    channel_id         TEXT NOT NULL,
    name               TEXT NOT NULL,
    command_id         TEXT NOT NULL REFERENCES custom_commands(id),
    published_by       TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled', 'orphaned')),
    cooldown_overrides TEXT,
    required_role      TEXT,
    last_run_version   INTEGER,
    created_at         INTEGER NOT NULL,
    PRIMARY KEY (channel_id, name)
);

-- ── Variables (ADR-0010, variable-access-matrix.md) ─────────────────────────
CREATE TABLE variables (
    ns          TEXT NOT NULL CHECK (ns IN ('chatter', 'channel', 'channel.chatter', 'publisher',
                                            'publisher.chatter', 'publisher.channel',
                                            'publisher.channel.chatter')),
    key1        TEXT NOT NULL,
    key2        TEXT NOT NULL DEFAULT '',
    key3        TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL,
    value       TEXT NOT NULL,               -- JSON
    updated_at  INTEGER NOT NULL,
    updated_by  TEXT,
    updated_via TEXT,
    PRIMARY KEY (ns, key1, key2, key3, name)
);
CREATE INDEX ix_variables_board ON variables(ns, key1, key2, name);

CREATE TABLE publication_write_grants (
    channel_id       TEXT NOT NULL,
    publication_name TEXT NOT NULL,
    variable         TEXT NOT NULL,          -- exact name, e.g. 'channel.chatter.points'; no wildcards
    granted_by       TEXT NOT NULL,
    granted_at       INTEGER NOT NULL,
    PRIMARY KEY (channel_id, publication_name, variable),
    FOREIGN KEY (channel_id, publication_name)
        REFERENCES custom_command_publications(channel_id, name) ON DELETE CASCADE
);

-- ── Triggers, timers, listeners (architecture §7) ───────────────────────────
CREATE TABLE triggers (
    id             INTEGER PRIMARY KEY,
    channel_id     TEXT NOT NULL,
    type           TEXT NOT NULL,            -- redemption|raid|sub|resub|gift_sub|cheer|follow|stream_online|stream_offline|timer|listener
    match          TEXT NOT NULL DEFAULT '{}',
    schedule       TEXT,
    expr           TEXT NOT NULL,
    syntax_version TEXT NOT NULL,
    run_as_rank    INTEGER NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 1,
    log_level      TEXT NOT NULL DEFAULT 'output',
    created_by     TEXT NOT NULL,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);
CREATE INDEX ix_triggers_channel_type ON triggers(channel_id, type);

-- ── Badword filter (architecture §9) ────────────────────────────────────────
CREATE TABLE filters (
    id          INTEGER PRIMARY KEY,
    channel_id  TEXT NOT NULL,               -- '*' = global
    pattern     TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('word', 'wildcard', 'regex', 'allow')),
    category    TEXT,
    action      TEXT NOT NULL CHECK (action IN ('mask', 'replace', 'tag', 'block')),
    replacement TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_by  TEXT,
    created_at  INTEGER NOT NULL
);
CREATE INDEX ix_filters_channel ON filters(channel_id, enabled);

-- ── Audit log (always on) ───────────────────────────────────────────────────
CREATE TABLE audit_log (
    id            INTEGER PRIMARY KEY,
    channel_id    TEXT,
    actor_user_id TEXT,
    via           TEXT NOT NULL,             -- chat | api | web | system
    action        TEXT NOT NULL,
    target        TEXT,
    before        TEXT,
    after         TEXT,
    at            INTEGER NOT NULL
);
CREATE INDEX ix_audit_channel_time ON audit_log(channel_id, at);

-- ── Built-in role seeds (global) ────────────────────────────────────────────
INSERT INTO roles (channel_id, name, rank, builtin, created_at) VALUES
    ('*', 'everyone',          0, 1, 0),
    ('*', 'subscriber',       20, 1, 0),
    ('*', 'vip',              60, 1, 0),
    ('*', 'moderator',        80, 1, 0),
    ('*', 'lead_moderator',   90, 1, 0),
    ('*', 'broadcaster',     100, 1, 0),
    ('*', 'bot_admin',      1000, 1, 0),
    ('*', 'bot_owner',     10000, 1, 0);
