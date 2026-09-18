-- Custom commands (ADR-0009): the owner's login for display, and write grants that follow the
-- command instead of the published name, so republishing a different command under the same name
-- cannot inherit the old grants (variable-access-matrix.md §4).
ALTER TABLE custom_commands ADD COLUMN owner_login TEXT;

CREATE TABLE publication_write_grants_new (
    channel_id TEXT NOT NULL,
    command_id TEXT NOT NULL REFERENCES custom_commands(id) ON DELETE CASCADE,
    variable   TEXT NOT NULL,          -- exact name, e.g. 'channel.chatter.points'; no wildcards
    granted_by TEXT NOT NULL,
    granted_at INTEGER NOT NULL,
    PRIMARY KEY (channel_id, command_id, variable)
);

INSERT INTO publication_write_grants_new (channel_id, command_id, variable, granted_by, granted_at)
SELECT g.channel_id, p.command_id, g.variable, g.granted_by, g.granted_at
  FROM publication_write_grants g
  JOIN custom_command_publications p
    ON p.channel_id = g.channel_id AND p.name = g.publication_name;

DROP TABLE publication_write_grants;
ALTER TABLE publication_write_grants_new RENAME TO publication_write_grants;

CREATE INDEX ix_cc_publications_command ON custom_command_publications(command_id);
CREATE INDEX ix_cc_links_command ON custom_command_links(command_id);
CREATE INDEX ix_cc_owner ON custom_commands(owner_user_id, status);
