"""The audit log: what write_audit records is what read_audit gives back (architecture §5.5)."""

from __future__ import annotations

from doomtp_bot.audit.log import read_audit, write_audit
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
