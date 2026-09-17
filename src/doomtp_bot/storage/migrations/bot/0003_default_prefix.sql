-- The default command sign is now 🏜 (U+1F3DC). Channels that never chose one follow it;
-- a channel that picked its own prefix keeps it.
UPDATE channels SET prefix = '🏜' WHERE prefix = '!';
