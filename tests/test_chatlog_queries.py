"""Shared reads over the chat log (architecture §3.3)."""

from __future__ import annotations

from doomtp_bot.chatlog.queries import search_messages
from doomtp_bot.storage.db import Databases

CHANNEL_ID = "100"


async def _say(dbs: Databases, message_id: str, text: str, at: int, **flags: object) -> None:
    columns = ", ".join(flags)
    await dbs.chatlog.execute(
        "INSERT INTO messages (message_id, channel_id, user_id, user_login, text, sent_at, received_at"
        + (f", {columns}" if flags else "")
        + ") VALUES (%s, %s, '400', 'alice', %s, %s, %s"
        + ", %s" * len(flags)
        + ")",
        (message_id, CHANNEL_ID, text, at, at, *flags.values()),
    )


async def test_search_finds_the_newest_first_and_chat_sees_only_what_is_still_in_chat(dbs: Databases) -> None:
    await _say(dbs, "m1", "the Doom speedrun was great", 1_000)
    await _say(dbs, "m2", "doom again, accents: dôôm", 2_000)
    await _say(dbs, "m3", "doom slur, removed by a mod", 3_000, deleted_at=3_500)
    await _say(dbs, "m4", "doom from someone timed out", 4_000, cleared_at=4_500)
    await _say(dbs, "m5", "the bot saying doom", 5_000, is_self=True)
    await _say(dbs, "m6", "!logsearch doom", 6_000, is_command=True)
    await dbs.chatlog.execute(
        "INSERT INTO messages (message_id, channel_id, user_id, user_login, text, sent_at, received_at)"
        " VALUES ('elsewhere', '200', '400', 'alice', 'doom elsewhere', 7000, 7000)"
    )

    everything = await search_messages(dbs.chatlog, CHANNEL_ID, "doom")
    assert [m["message_id"] for m in everything] == ["m6", "m5", "m4", "m3", "m2", "m1"]
    visible = await search_messages(dbs.chatlog, CHANNEL_ID, "doom", visible_only=True)
    assert [m["message_id"] for m in visible] == ["m2", "m1"]
    assert [m["message_id"] for m in await search_messages(dbs.chatlog, CHANNEL_ID, "doom", limit=1)] == [
        "m6"
    ]
    assert await search_messages(dbs.chatlog, CHANNEL_ID, '"') == []  # takes whatever a person types
