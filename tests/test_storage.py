from pathlib import Path

from doomtp_bot.storage.db import Databases, current_version, load_migrations, migrate


async def test_migrations_apply_and_are_idempotent(tmp_path: Path) -> None:
    dbs = await Databases.open(tmp_path / "bot.db", tmp_path / "chatlog.db")
    try:
        assert await current_version(dbs.bot) == len(load_migrations("bot"))
        assert await current_version(dbs.chatlog) == len(load_migrations("chatlog"))
        # Running again is a no-op.
        assert await migrate(dbs.bot, "bot") == len(load_migrations("bot"))
    finally:
        await dbs.close()


async def test_builtin_roles_seeded(tmp_path: Path) -> None:
    dbs = await Databases.open(tmp_path / "bot.db", tmp_path / "chatlog.db")
    try:
        async with dbs.bot.execute(
            "SELECT name, rank FROM roles WHERE channel_id = '*' ORDER BY rank"
        ) as cur:
            rows = [(r["name"], r["rank"]) for r in await cur.fetchall()]
        assert rows[0] == ("everyone", 0)
        assert ("lead_moderator", 90) in rows
        assert rows[-1] == ("bot_owner", 10000)
    finally:
        await dbs.close()


async def test_chatlog_fts_tracks_inserts(tmp_path: Path) -> None:
    dbs = await Databases.open(tmp_path / "bot.db", tmp_path / "chatlog.db")
    try:
        await dbs.chatlog.execute(
            "INSERT INTO messages (message_id, channel_id, user_id, user_login, text, sent_at, received_at)"
            " VALUES ('m1', 'c1', 'u1', 'alice', 'the doom slayer approaches', 1, 1)"
        )
        await dbs.chatlog.commit()
        async with dbs.chatlog.execute(
            "SELECT m.message_id FROM messages_fts f JOIN messages m ON m.rowid = f.rowid"
            " WHERE messages_fts MATCH 'slayer'"
        ) as cur:
            assert [r[0] for r in await cur.fetchall()] == ["m1"]
    finally:
        await dbs.close()
