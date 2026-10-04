"""The `pyramid` pack: what it does with the pyramid watcher's trigger events, and `!pyramid` (ADR-0028).

The events are fed straight to `pyramid_on_event` in a trigger context, the way `TriggerRunner` runs the
channel's `!event add pyramid pyramid_on_event`. The watcher itself is tested in `tests/watchers/`.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from doomtp_bot.lang.parser import Context
from doomtp_bot.runtime.engine import RunReport
from doomtp_bot.storage.db import fetch_value
from scripts.starter_pack import PYRAMID_FACTS, PYRAMID_PACK
from tests.customcmds.test_customcmds import BADGES, CHANNEL_ID, CHANNEL_LOGIN, USERS
from tests.customcmds.test_packs import OTHER_CHANNEL, Harness, h  # noqa: F401
from tests.test_starter_pack import OWNER, _install


def _chatter(harness: Harness, who: str, channel: str = CHANNEL_ID) -> Any:
    user = USERS[who]
    badges = frozenset(BADGES.get(who, set()))
    return harness.policy.build_chatter(channel, user["id"], user["name"], user["display"], badges)


def _info(harness: Harness, channel: str = CHANNEL_ID) -> Any:
    login = CHANNEL_LOGIN if channel == CHANNEL_ID else "other"
    return dataclasses.replace(harness.policy.channel_info(channel, login), prefix="!")


async def _say(harness: Harness, who: str, text: str) -> RunReport:
    ctx = harness.runtime.make_context(channel=_info(harness), invoker=_chatter(harness, who))
    report = await harness.runtime.run(text, ctx)
    assert report is not None
    return report


async def _event(harness: Harness, phase: str, *, chatter: str = "alice", **fields: Any) -> str | None:
    """One pyramid event, with alice building a LUL pyramid unless `fields` say otherwise."""
    event: dict[str, Any] = {
        "type": "pyramid",
        "pyramid_id": "p1",
        "phase": phase,
        "direction": "up" if phase == "step" else "",
        "token": "LUL",
        "width": 2,
        "peak": 2,
        "user": {"id": "400", "name": "alice", "display": "Alice"},
        "by_bot": False,
        "self_broken": False,
        **fields,
    }
    ctx = harness.runtime.make_context(
        channel=_info(harness),
        invoker=_chatter(harness, chatter),
        context=Context.TRIGGER,
        trigger_type="pyramid",
        run_as_rank=80,
        event=event,
    )
    report = await harness.runtime.run("pyramid_on_event", ctx)
    assert report is not None
    assert report.result.code == 0, report.result
    return report.send


async def _var(harness: Harness, name: str, ns: str = "publisher.channel", key2: str = CHANNEL_ID) -> Any:
    value = await fetch_value(
        harness.dbs.bot,
        "SELECT value FROM variables WHERE ns = %s AND key1 = %s AND key2 = %s AND key3 = '' AND name = %s",
        (ns, OWNER["id"], key2, name),
    )
    return None if value is None else json.loads(value)


async def _setup(harness: Harness, *settings: str) -> None:
    done = await _install(harness)
    assert f"create pack {PYRAMID_PACK}" in done
    await _set(harness, *settings)


async def _set(harness: Harness, *settings: str) -> None:
    for setting in settings:
        assert (await _say(harness, "mod", f"!pyramid {setting}")).result.code == 0, setting


async def test_install_writes_the_shared_facts_once(h: Harness) -> None:  # noqa: F811
    done = await _install(h)
    assert f"set the {len(PYRAMID_FACTS)} shared pyramid facts" in done
    assert await _var(h, "pyramid_facts", ns="publisher", key2="") == list(PYRAMID_FACTS)
    again = await _install(h)
    assert not any("pyramid facts" in step for step in again)


async def test_chance_zero_watches_without_breaking(h: Harness) -> None:  # noqa: F811
    await _setup(h)
    for width in (2, 3):
        assert await _event(h, "step", width=width, peak=width) is None
    assert await _var(h, "pyramid") is None


async def test_chance_hundred_breaks_once_per_pyramid_with_a_fact(h: Harness) -> None:  # noqa: F811
    await _setup(h, "chance 100")
    fact = await _event(h, "step")
    assert fact in PYRAMID_FACTS
    assert await _event(h, "step", width=3, peak=3) is None  # its line is already out
    assert (await _var(h, "pyramid"))["attempt"] == "p1"
    assert await _event(h, "step", pyramid_id="p2") in PYRAMID_FACTS  # the next pyramid gets its own try


async def test_a_bot_break_and_a_dodge_are_counted(h: Harness) -> None:  # noqa: F811
    await _setup(h, "chance 100")
    await _event(h, "step")
    breaker = {"id": "999", "name": "bot", "display": "Bot"}
    assert await _event(h, "broken", by_bot=True, breaker=breaker) is None
    await _event(h, "step", pyramid_id="p2")
    assert await _event(h, "complete", pyramid_id="p2", width=1, peak=3) == "Alice built a 3-wide LUL pyramid!"
    stats = await _var(h, "pyramid_stats")
    assert stats["broken_by_bot"] == 1 and stats["dodged"] == 1 and stats["completed"] == 1
    said = (await _say(h, "bob", "!pyramid")).send
    assert said == (
        "pyramids: 1 built, 1 broken by me, 0 broken by chat, 0 fumbled, 1 got past me. Biggest: 3 wide by Alice"
    )


async def test_completions_below_the_minimum_peak_are_ignored(h: Harness) -> None:  # noqa: F811
    await _setup(h, "minpeak 4")
    assert await _event(h, "complete", width=1, peak=3) is None
    assert await _var(h, "pyramid_stats") is None
    assert await _event(h, "complete", width=1, peak=4) == "Alice built a 4-wide LUL pyramid!"


async def test_the_congratulations_can_change_or_go_quiet(h: Harness) -> None:  # noqa: F811
    # Typed with the braces escaped, so they reach the template instead of filling in now.
    await _setup(h, r"congrats GG \{event.user.display\}, \{event.peak\} wide")
    assert await _event(h, "complete", width=1, peak=3) == "GG Alice, 3 wide"
    await _set(h, "congrats off")
    assert await _event(h, "complete", pyramid_id="p2", width=1, peak=3) is None
    assert (await _var(h, "pyramid_stats"))["completed"] == 2  # still counted
    await _set(h, "congrats reset")
    assert await _event(h, "complete", pyramid_id="p3", width=1, peak=3) == "Alice built a 3-wide LUL pyramid!"


async def test_breaks_by_chatters_count_for_the_breaker(h: Harness) -> None:  # noqa: F811
    await _setup(h)
    bob = {"id": "401", "name": "bob", "display": "Bob"}
    assert await _event(h, "broken", chatter="bob", breaker=bob) is None
    assert await _event(h, "broken", pyramid_id="p2", self_broken=True, breaker=USERS["alice"]) is None
    await _event(h, "complete", pyramid_id="p3", width=1, peak=3)
    stats = await _var(h, "pyramid_stats")
    assert stats["broken_by_chatters"] == 1 and stats["fumbled"] == 1
    breakers = (await _say(h, "alice", "!pyramid top breakers")).send
    assert breakers is not None and breakers.startswith("top pyramid breakers: 1.") and breakers.endswith(" 1")
    builders = (await _say(h, "alice", "!pyramid top")).send
    assert builders is not None and builders.startswith("top pyramid builders: 1.")


async def test_exempt_builders_are_never_broken(h: Harness) -> None:  # noqa: F811
    await _setup(h, "chance 100", "exempt mod")
    assert await _event(h, "step", chatter="mod") is None
    assert await _event(h, "step", chatter="alice") in PYRAMID_FACTS
    await _set(h, "exempt off")
    assert await _event(h, "step", chatter="mod", pyramid_id="p2") in PYRAMID_FACTS


async def test_channel_facts_and_the_shared_switch(h: Harness) -> None:  # noqa: F811
    await _setup(h, "sharedfacts off")
    empty = await _say(h, "alice", "!pyramid fact")
    assert empty.send == "no pyramid facts yet. Add one with !pyramid addfact <fact>"
    await _set(h, "chance 100")
    assert await _event(h, "step") is None  # nothing to say, so no try
    assert (await _var(h, "pyramid")).get("attempt") is None

    assert (await _say(h, "mod", "!pyramid addfact Pyramids are pointy")).send == "added pyramid fact #1"
    assert (await _say(h, "alice", "!pyramid fact")).send == "Pyramids are pointy"
    assert (await _say(h, "alice", "!pyramid fact 1")).send == "#1: Pyramids are pointy"
    assert await _event(h, "step") == "Pyramids are pointy"
    assert (await _say(h, "mod", "!pyramid delfact 1")).send == "deleted pyramid fact #1"
    assert (await _say(h, "mod", "!pyramid delfact 1")).send == "there is no pyramid fact #1"


async def test_settings_take_moderator_rank_and_valid_values(h: Harness) -> None:  # noqa: F811
    await _setup(h)
    for setting in ("chance 50", "exempt mod", "addfact x", "delfact 1", "congrats off"):
        assert (await _say(h, "alice", f"!pyramid {setting}")).send == "changing pyramid settings takes moderator rank"
    assert (await _say(h, "mod", "!pyramid chance 101")).send == "usage: pyramid chance <0-100>"
    assert (await _say(h, "mod", "!pyramid minpeak 1")).send == "usage: pyramid minpeak <2-50>"
    assert (
        await _say(h, "mod", "!pyramid exempt admins")
    ).send == "usage: pyramid exempt <off|sub|vip|mod|broadcaster>"
    assert (await _say(h, "mod", "!pyramid chance 25")).send == "pyramid break chance is now 25% per row"
    assert (await _say(h, "mod", "!pyramid exempt vip")).send == "vip and up are exempt from pyramid breaks"
    assert (await _say(h, "alice", "!pyramid settings")).send == (
        "break chance 25% per row, pyramids count from 3 wide, exempt: vip, shared facts: true,"
        " channel facts: 0, congratulations: on"
    )


async def test_typed_by_hand_on_event_does_nothing(h: Harness) -> None:  # noqa: F811
    await _setup(h, "chance 100")
    report = await _say(h, "alice", "!pyramid_on_event")
    assert report.result.code == 0 and report.send is None


async def test_settings_and_stats_are_per_channel(h: Harness) -> None:  # noqa: F811
    await _setup(h, "chance 100")
    ctx = h.runtime.make_context(channel=_info(h, OTHER_CHANNEL), invoker=_chatter(h, "alice", OTHER_CHANNEL))
    other = await h.runtime.run("!pyramid settings", ctx)
    assert other is not None and other.send is not None and other.send.startswith("break chance 0%")
