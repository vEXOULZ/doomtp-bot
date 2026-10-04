"""The audit log: what write_audit records is what read_audit gives back (architecture §5.5), in vex-platform's
shared `public.audit_log` (ADR-0027)."""

from __future__ import annotations

import datetime as dt

from doomtp_bot.audit.log import copy_legacy_audit, read_audit, write_audit
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.storage.db import Databases


async def test_rows_come_back_newest_first_with_values_decoded(dbs: Databases) -> None:
    await write_audit(dbs.bot, action="a.first", actor_user_id="1", via="chat", channel_id="100", after=[1])
    await write_audit(
        dbs.bot, action="a.second", actor_user_id=None, via="api", channel_id="100",
        target="x", before={"on": True}, after={"on": False},
    )  # fmt: skip

    rows = await read_audit(dbs.bot)
    assert [r["action"] for r in rows] == ["a.second", "a.first"]
    assert rows[0]["before"] == {"on": True} and rows[0]["after"] == {"on": False}
    assert rows[0]["target"] == "x" and rows[0]["via"] == "api" and rows[0]["actor_user_id"] is None
    assert rows[1]["before"] is None and rows[1]["after"] == [1]
    assert set(rows[0]) == {
        "id",
        "channel_id",
        "actor_user_id",
        "via",
        "action",
        "target",
        "before",
        "after",
        "at",
    }


async def test_a_value_that_is_not_json_is_returned_as_stored(dbs: Databases) -> None:
    await write_audit(dbs.bot, action="old", actor_user_id=None, via="chat", before="plain text, not json")
    (row,) = await read_audit(dbs.bot)
    assert row["before"] == "plain text, not json"


async def test_reading_one_channel_and_a_limit(dbs: Databases) -> None:
    for n in range(3):
        await write_audit(dbs.bot, action=f"here.{n}", actor_user_id=None, via="chat", channel_id="100")
    await write_audit(dbs.bot, action="elsewhere", actor_user_id=None, via="chat", channel_id="200")
    await write_audit(dbs.bot, action="global", actor_user_id=None, via="chat", channel_id=GLOBAL)

    assert [r["action"] for r in await read_audit(dbs.bot, channel_id="100")] == [
        "here.2",
        "here.1",
        "here.0",
    ]
    assert [r["action"] for r in await read_audit(dbs.bot, limit=2)] == ["global", "elsewhere"]
    assert (await read_audit(dbs.bot, limit=1))[0]["channel_id"] is None  # GLOBAL is stored as no channel


async def test_rows_land_in_the_shared_table_in_its_shape(dbs: Databases) -> None:
    await write_audit(dbs.bot, action="cc.create", actor_user_id="1", via="chat", channel_id="100", after={"a": 1})
    await write_audit(dbs.bot, action="http_limits", actor_user_id=None, via="api", before="3")  # undotted: older
    async with await dbs.bot.execute(
        "SELECT actor_kind, actor_id, via, action, scope, outcome, before, after FROM public.audit_log ORDER BY id"
    ) as cur:
        rows = [tuple(r.values()) for r in await cur.fetchall()]
    assert rows == [
        ("user", "1", "chat", "cc.create", "100", "ok", None, {"a": 1}),
        ("system", None, "api", "http_limits", None, "ok", 3, None),  # a string that is JSON is stored parsed
    ]
    async with await dbs.bot.execute("SELECT count(*) AS n FROM bot.audit_log") as cur:
        assert await cur.fetchone() == {"n": 0}


async def test_a_surface_the_shared_table_lacks_is_kept_in_detail(dbs: Databases) -> None:
    await write_audit(dbs.bot, action="pack.publish", actor_user_id=None, via="script")
    await write_audit(dbs.bot, action="channel.join", actor_user_id=None, via="irc")
    async with await dbs.bot.execute("SELECT via, detail FROM public.audit_log ORDER BY id") as cur:
        assert [tuple(r.values()) for r in await cur.fetchall()] == [
            ("cli", None),
            ("system", {"via": "irc"}),
        ]


async def test_rows_another_writer_left_have_no_user(dbs: Databases) -> None:
    """vex-platform's own writers (a refused API call, a job) name an actor that is not a Twitch user."""
    await dbs.bot.execute(
        "INSERT INTO public.audit_log (actor_kind, actor_id, via, action, outcome)"
        " VALUES ('api_key', 'ci', 'api', 'request.denied', 'denied')"
    )
    (row,) = await read_audit(dbs.bot)
    assert row["actor_user_id"] is None and row["action"] == "request.denied"
    assert await read_audit(dbs.bot, actor_user_id="ci") == []


async def test_the_old_tables_rows_are_copied_once(dbs: Databases) -> None:
    at = 1_790_006_400_123  # 2026-09-21 16:00:00.123 UTC
    await dbs.bot.execute(
        "INSERT INTO bot.audit_log (channel_id, actor_user_id, via, action, target, before, after, at) VALUES"
        " ('100', '1', 'chat', 'cc.create', 'cmd1', NULL, '{\"name\":\"hi\"}', %(at)s),"
        " (NULL, NULL, 'system', 'http_limits', NULL, 'plain text', '', %(at)s),"
        " ('100', '2', 'irc', 'role.create', 'mods', NULL, '7', %(at)s),"
        " (NULL, NULL, 'script', 'pack.publish', 'core', NULL, NULL, %(at)s)",
        {"at": at},
    )  # fmt: skip
    await write_audit(dbs.bot, action="cc.delete", actor_user_id="1", via="chat", channel_id="100")

    assert await copy_legacy_audit(dbs.bot) == 4
    assert await copy_legacy_audit(dbs.bot) == 0
    rows = await read_audit(dbs.bot)
    assert [(r["action"], r["channel_id"], r["actor_user_id"], r["before"], r["after"], r["at"]) for r in rows] == [
        ("pack.publish", None, None, None, None, at),
        ("role.create", "100", "2", None, 7, at),
        ("http_limits", None, None, "plain text", "", at),
        ("cc.create", "100", "1", None, {"name": "hi"}, at),
        ("cc.delete", "100", "1", None, None, rows[-1]["at"]),
    ]
    async with await dbs.bot.execute(
        "SELECT via, detail, request_id, at FROM public.audit_log WHERE action = 'role.create'"
    ) as cur:
        copied = await cur.fetchone()
    assert copied is not None
    assert (copied["via"], copied["detail"]) == ("system", {"via": "irc"})  # a surface the shared table lacks
    assert copied["request_id"].startswith("bot.audit_log:")
    assert copied["at"] == dt.datetime(2026, 9, 21, 16, 0, 0, 123000, tzinfo=dt.UTC)
