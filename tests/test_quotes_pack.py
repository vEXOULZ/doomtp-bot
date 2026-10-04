"""The `quotes` pack: `!quote` as commands the bot publishes, kept in `publisher.channel.quotes` (ADR-0019).

The migration that moves the old `quotes` table there is in `tests/test_schema.py`.
"""

from __future__ import annotations

import dataclasses
import json
import re

from doomtp_bot.filters.service import FilterService
from doomtp_bot.runtime.engine import RunReport
from doomtp_bot.runtime.result import Code
from doomtp_bot.storage.db import fetch_value
from scripts.starter_pack import QUOTES_PACK
from tests.customcmds.test_customcmds import BADGES, CHANNEL_ID, CHANNEL_LOGIN, USERS
from tests.customcmds.test_packs import OTHER_CHANNEL, Harness, h  # noqa: F401
from tests.test_starter_pack import OWNER, _install


async def _run(harness: Harness, who: str, text: str, *, live: bool = False, channel: str = CHANNEL_ID) -> RunReport:
    login = CHANNEL_LOGIN if channel == CHANNEL_ID else "other"
    info = dataclasses.replace(harness.policy.channel_info(channel, login), prefix="!", live=live, game="Doom")
    user = USERS[who]
    chatter = harness.policy.build_chatter(
        channel, user["id"], user["name"], user["display"], frozenset(BADGES.get(who, set()))
    )
    report = await harness.runtime.run(text, harness.runtime.make_context(channel=info, invoker=chatter))
    assert report is not None
    return report


async def _stored(harness: Harness, channel: str = CHANNEL_ID) -> dict[str, dict[str, str]]:
    value = await fetch_value(
        harness.dbs.bot,
        "SELECT value FROM variables WHERE ns = 'publisher.channel' AND key1 = %s AND key2 = %s AND name = 'quotes'",
        (OWNER["id"], channel),
    )
    return {} if value is None else json.loads(value)


async def test_quotes_are_numbered_and_read_back(h: Harness) -> None:  # noqa: F811
    done = await _install(h)
    assert f"create pack {QUOTES_PACK}" in done

    empty = await _run(h, "alice", "!quote")
    assert empty.result is not None and empty.result.code == Code.NOT_FOUND
    assert empty.send == "no quotes yet. Add one with !quote add <text>"

    # the text is kept as typed (`{arg.1+raw}`): quote marks, runs of spaces, and the escape a | needs
    assert (await _run(h, "mod", '!quote add I meant   to do "that"', live=True)).send == "added #1"
    assert (await _run(h, "mod", r"!quote add second \| one")).send == "added #2"

    first = await _run(h, "alice", "!quote 1")
    assert first.send is not None and re.fullmatch(r'#1: I meant   to do "that" \[Doom, \d{4}-\d\d-\d\d\]', first.send)
    second = await _run(h, "alice", "!quote 2")  # not live when added: the date alone
    assert second.send is not None and re.fullmatch(r"#2: second \\\| one \[\d{4}-\d\d-\d\d\]", second.send)
    assert (await _run(h, "alice", "!quote")).send in (first.send, second.send)

    assert set(await _stored(h)) == {"1", "2"}
    assert (await _stored(h))["1"]["game"] == "Doom" and "game" not in (await _stored(h))["2"]


async def test_only_moderators_change_quotes_and_numbers_are_never_reused(h: Harness) -> None:  # noqa: F811
    await _install(h)
    denied = await _run(h, "alice", "!quote add mine")
    assert denied.result is not None and denied.result.code == Code.FAIL
    assert denied.send == "adding quotes takes moderator rank"
    await _run(h, "mod", "!quote add one")
    await _run(h, "mod", "!quote add two")
    assert (await _run(h, "alice", "!quote del 2")).send == "deleting quotes takes moderator rank"
    assert (await _run(h, "mod", "!quote delete 2")).send == "deleted #2"
    gone = await _run(h, "mod", "!quote del 2")
    assert gone.result is not None and gone.result.code == Code.NOT_FOUND
    assert (await _run(h, "alice", "!quote 2")).send == "there is no quote #2"
    assert (await _run(h, "mod", "!quote add three")).send == "added #3"  # #2 stays taken
    assert set(await _stored(h)) == {"1", "3"}


async def test_bad_input_gets_a_usage_line(h: Harness) -> None:  # noqa: F811
    await _install(h)
    too_long = await _run(h, "mod", "!quote add " + "x" * 401)
    assert too_long.send == "a quote is at most 400 characters"
    word = await _run(h, "alice", "!quote meant")  # search didn't come across from the module
    assert word.result is not None and word.result.code == Code.USAGE
    assert await _stored(h) == {}


async def test_members_are_internal(h: Harness) -> None:  # noqa: F811
    await _install(h)
    await _run(h, "mod", "!quote add one")
    typed = await _run(h, "alice", "!quote_show 1")
    assert typed.result is not None and typed.result.code == Code.UNKNOWN


async def test_quotes_are_kept_per_channel(h: Harness) -> None:  # noqa: F811
    await _install(h)
    await _run(h, "mod", "!quote add here")
    assert (await _run(h, "mod", "!quote add there", channel=OTHER_CHANNEL)).send == "added #1"
    assert (await _run(h, "alice", "!quote 1", channel=OTHER_CHANNEL)).send is not None
    assert (await _stored(h, OTHER_CHANNEL))["1"]["text"] == "there"
    assert (await _stored(h))["1"]["text"] == "here"


async def test_the_filter_keeps_blocked_words_out(h: Harness) -> None:  # noqa: F811
    filters = FilterService(h.dbs.bot)
    await filters.reload()
    await filters.add(
        channel_id=CHANNEL_ID, pattern="slur", kind="word", action="block", actor_user_id=None, via="chat"
    )
    h.runtime.services["filters"] = filters
    await _install(h)
    blocked = await _run(h, "mod", "!quote add a slur here")
    assert blocked.result is not None and not blocked.result.ok
    assert await _stored(h) == {}
