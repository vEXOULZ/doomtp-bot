"""The link rule for bot output (ADR-0019)."""

from __future__ import annotations

import json

import pytest

from doomtp_bot.chatlog.queries import latest_badges
from doomtp_bot.core.events import Badge
from doomtp_bot.core.links import BotBadges, defang_links
from doomtp_bot.storage.db import Databases


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("go to example.com now", "go to example dot com now"),
        ("https://www.twitch.tv/doomtp?x=1", "https://www dot twitch dot tv/doomtp?x=1"),
        ("clip at clips.twitch.tv/Abc-1.", "clip at clips dot twitch dot tv/Abc-1."),
        ("(sub.example.org)", "(sub dot example dot org)"),
        ("mail me@example.com", "mail me@example dot com"),
        ("version v1.2.3, 3.14, e.g. this", "version v1.2.3, 3.14, e.g. this"),
        ("no links here", "no links here"),
    ],
)
def test_defang_links(text: str, expected: str) -> None:
    assert defang_links(text) == expected


def test_bot_badges_follow_the_latest_message() -> None:
    badges = BotBadges()
    assert not badges.may_link("1")
    badges.saw("1", (Badge("vip", "1"),))
    assert badges.may_link("1")
    badges.saw("1", (Badge("subscriber", "3"),))  # the VIP was taken away
    assert not badges.may_link("1")
    badges.load([("2", json.dumps([{"set_id": "moderator", "id": "1", "info": ""}])), ("3", None), ("4", "{bad")])
    assert badges.may_link("2") and not badges.may_link("3") and not badges.may_link("4")


async def test_latest_badges_reads_the_newest_message_per_channel(dbs: Databases) -> None:
    rows = [
        ("m1", "100", "vip", 1_000),
        ("m2", "100", "subscriber", 2_000),
        ("m3", "200", "vip", 1_500),
    ]
    for message_id, channel_id, badge, at in rows:
        await dbs.chatlog.execute(
            "INSERT INTO messages (message_id, channel_id, user_id, user_login, text, raw, raw_format, sent_at,"
            " received_at, is_self) VALUES (%s, %s, '9', 'bot', 'hi', %s, 'eventsub', %s, %s, true)",
            (message_id, channel_id, json.dumps({"badges": [{"set_id": badge, "id": "1"}]}), at, at),
        )
    badges = BotBadges()
    badges.load(await latest_badges(dbs.chatlog, "9"))
    assert not badges.may_link("100") and badges.may_link("200")
