"""The public reference of triggers and `$` fields (triggers/reference.py) stays complete and true."""

from __future__ import annotations

from doomtp_bot.lang.parser import BOT_FIELDS
from doomtp_bot.triggers.reference import (
    BOT_FIELD_DOCS,
    BOT_ROOT_DOCS,
    EVENTS,
    NOTICE_TRIGGER_TYPE,
    trigger_payload,
)
from doomtp_bot.triggers.service import TRIGGER_TYPES


def test_every_trigger_type_and_bot_field_is_documented() -> None:
    assert tuple(EVENTS) == TRIGGER_TYPES
    assert {r: set(f) for r, f in BOT_FIELDS.items()} == {r: set(f) for r, f in BOT_FIELD_DOCS.items()}
    assert set(BOT_ROOT_DOCS) == set(BOT_FIELDS)


def test_a_raid_reads_as_the_docs_say() -> None:
    detail = {"user": {"id": "500", "login": "raider", "display": "Raider"}, "viewer_count": 42}
    payload = trigger_payload("raid", {"chatter": {"id": "500", "login": "raider"}, "detail": detail})
    assert payload["viewers"] == 42 and payload["user"] == {"id": "500", "name": "raider", "display": "Raider"}


def test_subs_and_gifts_read_as_the_docs_say() -> None:
    chatter = {"id": "7", "login": "sam"}
    resub = trigger_payload(
        "resub", {"chatter": chatter, "detail": {"tier": "1000", "cumulative_months": 14, "months": 1}}
    )
    assert (resub["months"], resub["tier"], resub["user"]["name"]) == (14, "1000", "sam")
    gift = trigger_payload(
        "sub_gift", {"chatter": None, "detail": {"tier": "2000", "recipient": {"id": "8", "login": "kim"}}}
    )
    assert gift["user"] is None and gift["recipient"]["name"] == "kim" and NOTICE_TRIGGER_TYPE["sub_gift"] == "gift_sub"


def test_notices_built_flat_are_left_alone() -> None:
    follow = {"user": {"id": "1", "name": "a", "display": "A"}}
    assert trigger_payload("follow", follow) is follow
