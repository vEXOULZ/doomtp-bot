"""The pyramid watcher (ADR-0028): rows, how a pyramid ends, and who ended it."""

from __future__ import annotations

from itertools import count
from typing import Any

from doomtp_bot.core.events import Badge, ChatMessage
from doomtp_bot.watchers import PyramidWatcher, WatchEvent
from doomtp_bot.watchers.pyramid import row_of

CHANNEL, OTHER = "100", "200"
USERS = {"alice": "400", "bob": "401", "bot": "999"}
# Built from code points so no editor or tool quietly drops them from the source.
TAG_SPACE, CGJ, ZWSP, VS16, HEART = chr(0xE0000), chr(0x34F), chr(0x200B), chr(0xFE0F), chr(0x2764)

_ids = count(1)


def line(who: str, text: str, *, channel: str = CHANNEL, badges: tuple[str, ...] = ()) -> ChatMessage:
    return ChatMessage(
        message_id=f"m{next(_ids)}", channel_id=channel, channel_login="doomtp", user_id=USERS[who],
        user_login=who, display_name=who.title(), text=text, sent_at=0, received_at=0,
        badges=tuple(Badge(b, "1") for b in badges), is_self=who == "bot",
    )  # fmt: skip


def feed(watcher: PyramidWatcher, *lines: ChatMessage) -> list[WatchEvent]:
    return [event for msg in lines for event in watcher.observe(msg)]


def rows(who: str, token: str, *widths: int, channel: str = CHANNEL) -> list[ChatMessage]:
    return [line(who, " ".join([token] * w), channel=channel) for w in widths]


def phases(events: list[WatchEvent]) -> list[tuple[Any, ...]]:
    return [(e.payload["phase"], e.payload["direction"], e.payload["width"]) for e in events]


def test_a_row_is_one_token_repeated() -> None:
    assert row_of("LUL LUL  LUL") == ("LUL", 3)
    assert row_of("LUL") == ("LUL", 1)
    assert row_of("LUL Kappa") is None
    assert row_of("   ") is None


def test_invisible_characters_are_not_part_of_a_row() -> None:
    assert row_of(f"LUL LUL {TAG_SPACE}") == ("LUL", 2)
    assert row_of(f"LUL{CGJ} LUL{ZWSP}") == ("LUL", 2)
    assert row_of(f"LUL LUL {VS16}") == ("LUL", 2)
    assert row_of(f"{HEART}{VS16} {HEART}{VS16}") == (f"{HEART}{VS16}", 2)  # an emoji keeps its own selector


def test_a_full_pyramid_steps_up_then_down_and_completes() -> None:
    w = PyramidWatcher()
    events = feed(w, *rows("alice", "LUL", 1, 2, 3, 2, 1))
    assert phases(events) == [("step", "up", 2), ("step", "up", 3), ("step", "down", 2), ("complete", "", 1)]
    done = events[-1]
    assert done.type == "pyramid" and done.channel_id == CHANNEL
    assert done.payload["peak"] == 3 and done.payload["token"] == "LUL"
    assert done.payload["user"] == {"id": "400", "name": "alice", "display": "Alice"}
    assert done.user == ("400", "alice", "Alice")
    assert len({e.payload["pyramid_id"] for e in events}) == 1


def test_the_builders_badges_ride_along_for_their_rank() -> None:
    w = PyramidWatcher()
    first = line("alice", "LUL", badges=("moderator",))
    events = feed(w, first, *rows("alice", "LUL", 2))
    assert events[0].badges == frozenset({"moderator"})
    assert events[0].payload["pyramid_id"] == first.message_id


def test_the_bot_breaks_it_when_its_line_lands_before_the_last_row() -> None:
    w = PyramidWatcher()
    events = feed(w, *rows("alice", "LUL", 1, 2, 3, 2), line("bot", "Fact: pyramids are old"), *rows("alice", "LUL", 1))
    broken = events[-1]
    assert broken.payload["phase"] == "broken"
    assert broken.payload["by_bot"] is True and broken.payload["self_broken"] is False
    assert broken.payload["breaker"]["name"] == "bot"
    assert broken.payload["peak"] == 3 and broken.payload["width"] == 2


def test_the_pyramid_completes_when_the_last_row_lands_before_the_bot() -> None:
    w = PyramidWatcher()
    events = feed(w, *rows("alice", "LUL", 1, 2, 3, 2, 1), line("bot", "Fact: pyramids are old"))
    assert events[-1].payload["phase"] == "complete"


def test_another_chatter_breaks_it() -> None:
    w = PyramidWatcher()
    events = feed(w, *rows("alice", "LUL", 1, 2, 3), line("bob", "no"))
    broken = events[-1].payload
    assert broken["phase"] == "broken" and broken["by_bot"] is False and broken["self_broken"] is False
    assert broken["breaker"] == {"id": "401", "name": "bob", "display": "Bob"}


def test_the_builder_fumbles_with_a_wrong_row() -> None:
    w = PyramidWatcher()
    for wrong in ("LUL LUL LUL LUL LUL", "Kappa Kappa", "lol"):
        events = feed(w, *rows("alice", "LUL", 1, 2, 3), line("alice", wrong))
        broken = events[-1].payload
        assert broken["phase"] == "broken" and broken["self_broken"] is True, wrong
        assert broken["breaker"]["id"] == "400"


def test_going_back_up_while_falling_is_a_fumble() -> None:
    w = PyramidWatcher()
    events = feed(w, *rows("alice", "LUL", 1, 2, 3, 2, 3))
    assert events[-1].payload["phase"] == "broken" and events[-1].payload["self_broken"] is True


def test_a_breaking_line_can_start_the_next_pyramid() -> None:
    w = PyramidWatcher()
    events = feed(w, *rows("alice", "LUL", 1, 2), *rows("bob", "Kappa", 1, 2, 1))
    assert [e.payload["phase"] for e in events] == ["step", "broken", "step", "complete"]
    assert events[-1].payload["user"]["name"] == "bob"


def test_the_bot_never_builds_a_pyramid() -> None:
    w = PyramidWatcher()
    assert feed(w, *rows("bot", "LUL", 1, 2, 1)) == []


def test_a_lone_one_wide_line_is_not_a_pyramid() -> None:
    w = PyramidWatcher()
    assert feed(w, line("alice", "LUL"), line("bob", "hi"), line("alice", "LUL"), line("alice", "LUL")) == []


def test_rows_with_invisible_characters_still_count() -> None:
    w = PyramidWatcher()
    texts = ["LUL", f"LUL LUL {TAG_SPACE}", f"LUL LUL LUL{CGJ}", f"LUL LUL {TAG_SPACE}", "LUL"]
    events = feed(w, *(line("alice", t) for t in texts))
    assert events[-1].payload["phase"] == "complete"


def test_channels_are_watched_separately() -> None:
    w = PyramidWatcher()
    a = rows("alice", "LUL", 1, 2, 3, 2, 1)
    b = rows("bob", "Kappa", 1, 2, 1, channel=OTHER)
    events = feed(w, a[0], b[0], a[1], b[1], a[2], b[2], a[3], a[4])
    assert [(e.channel_id, e.payload["phase"]) for e in events] == [
        (CHANNEL, "step"),
        (OTHER, "step"),
        (CHANNEL, "step"),
        (OTHER, "complete"),
        (CHANNEL, "step"),
        (CHANNEL, "complete"),
    ]


def test_forget_drops_a_pyramid_in_progress() -> None:
    w = PyramidWatcher()
    feed(w, *rows("alice", "LUL", 1, 2, 3))
    w.forget(CHANNEL)
    assert feed(w, *rows("alice", "LUL", 2, 1)) == []
