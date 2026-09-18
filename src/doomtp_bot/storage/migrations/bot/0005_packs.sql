-- Packs: named sets of custom commands that publish and unpublish as a unit (ADR-0012).
-- A channel stores one row per published pack, not one per member, so a member added later
-- appears immediately — the same live-edit rule as ADR-0009.
CREATE TABLE custom_command_packs (
    id            TEXT PRIMARY KEY,          -- e.g. pk_7f3k2
    owner_user_id TEXT NOT NULL,
    name          TEXT NOT NULL,             -- also the module name for !module toggles
    summary       TEXT,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted')),
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    UNIQUE (owner_user_id, name)
);

CREATE TABLE custom_command_pack_members (
    pack_id    TEXT NOT NULL REFERENCES custom_command_packs(id) ON DELETE CASCADE,
    command_id TEXT NOT NULL REFERENCES custom_commands(id) ON DELETE CASCADE,
    added_at   INTEGER NOT NULL,
    PRIMARY KEY (pack_id, command_id)
);

CREATE TABLE custom_command_pack_publications (
    channel_id   TEXT NOT NULL,              -- '*' is every channel (ADR-0012 derived commands)
    pack_id      TEXT NOT NULL REFERENCES custom_command_packs(id) ON DELETE CASCADE,
    published_by TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at   INTEGER NOT NULL,
    PRIMARY KEY (channel_id, pack_id)
);

CREATE INDEX ix_cc_pack_members_command ON custom_command_pack_members(command_id);
CREATE INDEX ix_cc_pack_publications_channel ON custom_command_pack_publications(channel_id, status);
