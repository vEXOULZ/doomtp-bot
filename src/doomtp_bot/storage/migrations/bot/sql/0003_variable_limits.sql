-- Storage limits per namespace owner (ADR-0019 "Storage limits", spec §6.5). Every variable belongs to
-- one owner: the chatter, channel or publisher named by the first segment of its namespace and key1.
-- size_bytes is the stored JSON's size, the same number the runtime checks, so usage is one SUM.
ALTER TABLE variables ADD COLUMN size_bytes integer GENERATED ALWAYS AS (octet_length(value)) STORED;

-- One row per override. owner_kind '*' with owner_id '*' holds the defaults every other owner falls
-- back to; a NULL column in an override means "use the default".
CREATE TABLE variable_limits (
    owner_kind      text NOT NULL CHECK (owner_kind IN ('*', 'chatter', 'channel', 'publisher')),
    owner_id        text NOT NULL,
    quota_bytes     bigint CHECK (quota_bytes >= 0),
    value_cap_bytes integer CHECK (value_cap_bytes >= 0),
    updated_at      bigint NOT NULL,
    updated_by      text,
    PRIMARY KEY (owner_kind, owner_id),
    CHECK ((owner_kind = '*') = (owner_id = '*'))
);
INSERT INTO variable_limits (owner_kind, owner_id, quota_bytes, value_cap_bytes, updated_at)
VALUES ('*', '*', 1048576, 262144, 0);
