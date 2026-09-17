import asyncio

from doomtp_bot.storage.db import Databases, current_version, load_migrations, migrate, transaction


async def test_migrations_apply_and_are_idempotent(dbs: Databases) -> None:
    assert await current_version(dbs.bot) == len(load_migrations("bot"))
    assert await current_version(dbs.chatlog) == len(load_migrations("chatlog"))
    # Running again is a no-op.
    assert await migrate(dbs.bot, "bot") == len(load_migrations("bot"))


async def test_builtin_roles_seeded(dbs: Databases) -> None:
    async with dbs.bot.execute("SELECT name, rank FROM roles WHERE channel_id = '*' ORDER BY rank") as cur:
        rows = [(r["name"], r["rank"]) for r in await cur.fetchall()]
    assert rows[0] == ("everyone", 0)
    assert ("lead_moderator", 90) in rows
    assert rows[-1] == ("bot_owner", 10000)


async def test_chatlog_fts_tracks_inserts(dbs: Databases) -> None:
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


async def test_transactions_sharing_a_connection_do_not_interleave(dbs: Databases) -> None:
    async def grant(i: int) -> None:
        async with transaction(dbs.bot, immediate=True):
            await dbs.bot.execute(
                "INSERT INTO global_admins (user_id, user_login, granted_by, granted_at) VALUES (?, ?, 'system', 0)",
                (str(i), f"user{i}"),
            )
            await asyncio.sleep(0)  # let the other writers run mid-transaction
            if i == 3:
                raise RuntimeError("boom")

    results = await asyncio.gather(*(grant(i) for i in range(6)), return_exceptions=True)
    assert [type(r).__name__ for r in results] == ["NoneType"] * 3 + ["RuntimeError"] + ["NoneType"] * 2
    async with dbs.bot.execute("SELECT user_id FROM global_admins ORDER BY user_id") as cur:
        assert [r[0] for r in await cur.fetchall()] == ["0", "1", "2", "4", "5"]
