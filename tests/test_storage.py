import asyncio

from doomtp_bot.storage.db import (
    Databases,
    current_version,
    execute,
    fetch_all,
    fetch_value,
    load_migrations,
    migrate,
    transaction,
)


async def test_migrations_apply_and_are_idempotent(dbs: Databases) -> None:
    assert await current_version(dbs.bot, "bot") == len(load_migrations("bot"))
    assert await current_version(dbs.chatlog, "chatlog") == len(load_migrations("chatlog"))
    # Running again is a no-op.
    assert await migrate(dbs.bot, "bot") == len(load_migrations("bot"))


async def test_each_migration_is_recorded_by_name(dbs: Databases) -> None:
    """`PRAGMA user_version` held one number; the table keeps the whole history (ADR-0014)."""
    rows = await fetch_all(dbs.bot, "SELECT version, name FROM schema_migrations ORDER BY version")
    assert [r["name"] for r in rows] == [m.name for m in load_migrations("bot")]


async def test_the_two_schemas_cannot_see_each_other(dbs: Databases) -> None:
    """One database, but each connection's search_path pins it to its own schema."""
    assert await fetch_value(dbs.bot, "SELECT current_schema()") == "bot"
    assert await fetch_value(dbs.chatlog, "SELECT current_schema()") == "chatlog"


async def test_builtin_roles_seeded(dbs: Databases) -> None:
    rows = await fetch_all(dbs.bot, "SELECT name, rank FROM roles WHERE channel_id = '*' ORDER BY rank")
    ranked = [(r["name"], r["rank"]) for r in rows]
    assert ranked[0] == ("everyone", 0)
    assert ("lead_moderator", 90) in ranked
    assert ranked[-1] == ("bot_owner", 10000)


async def _log(dbs: Databases, message_id: str, text: str) -> None:
    await execute(
        dbs.chatlog,
        "INSERT INTO messages (message_id, channel_id, user_id, user_login, text, sent_at, received_at)"
        " VALUES (%s, 'c1', 'u1', 'alice', %s, 1, 1)",
        (message_id, text),
    )


async def _search(dbs: Databases, query: str) -> list[str]:
    rows = await fetch_all(
        dbs.chatlog,
        "SELECT message_id FROM messages WHERE tsv @@ websearch_to_tsquery('simple', chatlog_unaccent(%s))"
        " ORDER BY message_id",
        (query,),
    )
    return [r["message_id"] for r in rows]


async def test_search_index_tracks_inserts(dbs: Databases) -> None:
    await _log(dbs, "m1", "the doom slayer approaches")
    assert await _search(dbs, "slayer") == ["m1"]
    assert await _search(dbs, "cyberdemon") == []


async def test_search_ignores_case_and_diacritics(dbs: Databases) -> None:
    """What FTS5's `unicode61 remove_diacritics 2` did, now done by unaccent (ADR-0014)."""
    await _log(dbs, "m1", "meeting at the CAFÉ")
    assert await _search(dbs, "cafe") == ["m1"]
    assert await _search(dbs, "CAFÉ") == ["m1"]


async def test_search_matches_phrases(dbs: Databases) -> None:
    await _log(dbs, "m1", "the doom slayer approaches")
    await _log(dbs, "m2", "the slayer of doom departs")
    assert await _search(dbs, '"doom slayer"') == ["m1"]


async def test_the_log_ignores_a_message_it_already_has(dbs: Databases) -> None:
    """Backfill re-offers what EventSub already logged; the first copy wins (ADR-0008)."""
    await _log(dbs, "m1", "first")
    await execute(
        dbs.chatlog,
        "INSERT INTO messages (message_id, channel_id, user_id, user_login, text, sent_at, received_at)"
        " VALUES ('m1', 'c1', 'u1', 'alice', 'second', 2, 2) ON CONFLICT DO NOTHING",
    )
    assert await fetch_value(dbs.chatlog, "SELECT text FROM messages WHERE message_id = 'm1'") == "first"


async def test_transactions_sharing_a_connection_do_not_interleave(dbs: Databases) -> None:
    async def grant(i: int) -> None:
        async with transaction(dbs.bot):
            await execute(
                dbs.bot,
                "INSERT INTO global_admins (user_id, user_login, granted_by, granted_at)"
                " VALUES (%s, %s, 'system', 0)",
                (str(i), f"user{i}"),
            )
            await asyncio.sleep(0)  # let the other writers run mid-transaction
            if i == 3:
                raise RuntimeError("boom")

    results = await asyncio.gather(*(grant(i) for i in range(6)), return_exceptions=True)
    assert [type(r).__name__ for r in results] == ["NoneType"] * 3 + ["RuntimeError"] + ["NoneType"] * 2
    rows = await fetch_all(dbs.bot, "SELECT user_id FROM global_admins ORDER BY user_id")
    assert [r["user_id"] for r in rows] == ["0", "1", "2", "4", "5"]
