-- AutoMod-assisted filtering (architecture §9.3): acting on *incoming* chat that the channel's own
-- filter would refuse to send. Off everywhere until a moderator turns it on, and it needs the
-- moderator tier to do anything at all.
ALTER TABLE channels ADD COLUMN automod_action    TEXT    NOT NULL DEFAULT 'off';  -- off | delete | timeout
ALTER TABLE channels ADD COLUMN automod_timeout_s INTEGER NOT NULL DEFAULT 600;
