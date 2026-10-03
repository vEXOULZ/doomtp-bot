"""The 1.0 → 2.0 rewrite of stored bodies (ADR-0018 item 7, scripts/migrate_v2.py)."""

from __future__ import annotations

from doomtp_bot.storage.db import Databases, fetch_all, fetch_one
from scripts.migrate_v2 import migrate

V1_BODY = "random 1-6 > channel.last && echo {chatter.display} rolled {1} || echo {_.code}"
V2_BODY = "random 1-6 -> channel.last && echo {$chatter.display} rolled {_1} || echo {_.code}"


async def seed(dbs: Databases) -> None:
    conn = dbs.bot
    for cid, name in (("cc_a", "roll"), ("cc_b", "check"), ("cc_c", "42")):
        await conn.execute(
            "INSERT INTO custom_commands (id, owner_user_id, owner_login, name, current_version, created_at,"
            " updated_at) VALUES (%s, 'u1', 'alice', %s, 1, 0, 0)",
            (cid, name),
        )
        await conn.execute(
            "INSERT INTO custom_command_versions (command_id, version, body, syntax_version, created_at)"
            " VALUES (%s, 1, %s, '1.0', 0)",
            (cid, V1_BODY if cid == "cc_a" else "echo hi"),
        )
    await conn.execute(
        "INSERT INTO triggers (channel_id, type, expr, syntax_version, run_as_rank, created_by, created_at,"
        " updated_at) VALUES ('c1', 'raid', 'echo hi {event.user.name} >> channel.raids', '1.0', 50, 'u1', 0, 0)"
    )
    await conn.execute(
        "INSERT INTO callbacks (channel_id, scope, kind, expr, syntax_version, updated_at)"
        " VALUES ('c1', 'channel', 'on_cooldown', 'echo wait {chatter.name}', '1.0', 0)"
    )


async def test_every_stored_body_moves_to_2_0(dbs: Databases) -> None:
    await seed(dbs)
    report = await migrate(dbs.bot)
    assert report.rewritten == {"custom command": 3, "trigger": 1, "callback": 1}
    assert report.broken == []
    assert report.reads_code == [f"custom command alice/roll v1: {V2_BODY}"]
    assert report.shadowed == [
        "alice/42: a bare expression line takes a number",
        "alice/check: the built-in shadows it",
    ]

    row = await fetch_one(dbs.bot, "SELECT body, syntax_version FROM custom_command_versions WHERE command_id = 'cc_a'")
    assert row == {"body": V2_BODY, "syntax_version": "2.0"}
    [trigger] = await fetch_all(dbs.bot, "SELECT expr, syntax_version FROM triggers")
    assert trigger == {"expr": "echo hi {event.user.name} --> channel.raids", "syntax_version": "2.0"}
    [callback] = await fetch_all(dbs.bot, "SELECT expr, syntax_version FROM callbacks")
    assert callback == {"expr": "echo wait {$chatter.name}", "syntax_version": "2.0"}

    again = await migrate(dbs.bot)  # a second run finds nothing, so `->` is never quoted twice
    assert again.rewritten == {"custom command": 0, "trigger": 0, "callback": 0}


async def test_a_dry_run_writes_nothing(dbs: Databases) -> None:
    await seed(dbs)
    report = await migrate(dbs.bot, dry_run=True)
    assert ("custom command", "alice/roll v1", V1_BODY, V2_BODY) in report.changes
    versions = await fetch_all(dbs.bot, "SELECT DISTINCT syntax_version FROM custom_command_versions")
    assert versions == [{"syntax_version": "1.0"}]


async def test_a_body_that_does_not_parse_stops_everything(dbs: Databases) -> None:
    await seed(dbs)
    await dbs.bot.execute("UPDATE callbacks SET expr = 'echo {'")
    report = await migrate(dbs.bot)
    assert len(report.broken) == 1 and report.broken[0].startswith("callback c1 channel on_cooldown:")
    versions = await fetch_all(dbs.bot, "SELECT DISTINCT syntax_version FROM triggers")
    assert versions == [{"syntax_version": "1.0"}]
