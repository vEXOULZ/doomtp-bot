-- Internal pack members and system packs (ADR-0019 "Sentinels", ADR-0012 amendment).
-- An internal member is callable only from the bodies of its own pack's commands: it is never offered to
-- a channel, so typing it is an unknown command, and help doesn't list it.
ALTER TABLE custom_command_pack_members ADD COLUMN internal boolean NOT NULL DEFAULT false;

-- A system pack is bot-owned and installed by script. Its members resolve with the sentinels, before
-- every other name, and can't be toggled, shadowed or published. The column is its version, which
-- startup compares with the one the code expects; NULL is an ordinary pack.
ALTER TABLE custom_command_packs ADD COLUMN system_version integer CHECK (system_version > 0);
CREATE UNIQUE INDEX ux_cc_system_pack_name ON custom_command_packs(name)
    WHERE system_version IS NOT NULL AND status = 'active';
