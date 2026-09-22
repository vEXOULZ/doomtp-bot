"""Badword filter: normalization, matching, and the two places it applies (architecture §9)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from doomtp_bot.filters.matcher import ChannelFilter, FilterEntry, FilterError, compile_entry
from doomtp_bot.filters.normalize import normalize
from doomtp_bot.filters.service import FilterService
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.runtime.result import Code
from doomtp_bot.storage.db import Databases
from tests.customcmds.test_customcmds import TickingClock

CHANNEL = "100"


def entry(pattern: str, **kw: object) -> FilterEntry:
    return FilterEntry(id=kw.pop("id", 1), channel_id=CHANNEL, pattern=pattern, **kw)  # type: ignore[arg-type]


def censor(entries: list[FilterEntry], text: str) -> str | None:
    return ChannelFilter(entries).apply(text).text


# ── normalization ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Nice", "nice"),
        ("nïcé", "nice"),
        ("niiiice", "nice"),
        ("n​ice", "nice"),  # zero-width space
        ("ｎｉｃｅ", "nice"),  # full-width (NFKD)
        ("n1c3", "nice"),  # leetspeak
        ("𝓷𝓲𝓬𝓮", "nice"),  # mathematical alphanumerics
        ("ß", "s"),  # NFKD gives "ss", collapsed to one
    ],
)
def test_normalization_folds_the_usual_tricks(raw: str, expected: str) -> None:
    assert normalize(raw).text == expected


def test_normalized_positions_map_back_to_the_original() -> None:
    normalized = normalize("hey nïïce there")
    start = normalized.text.index("nice")
    first, last = normalized.span(start, start + 4)
    assert "hey nïïce there"[first:last] == "nïïce"


# ── matching ───────────────────────────────────────────────────────────────
def test_word_entries_need_a_word_boundary() -> None:
    entries = [entry("bad")]
    assert censor(entries, "that is bad") == "that is ***"
    assert censor(entries, "badminton is fine") == "badminton is fine"


def test_matching_sees_through_leetspeak_spacing_and_padding() -> None:
    entries = [entry("bad")]
    assert censor(entries, "that is b4d") == "that is ***"
    assert censor(entries, "that is b a d") == "that is *****"
    assert censor(entries, "that is baaaad") == "that is ******"


def test_allow_entries_solve_the_scunthorpe_problem() -> None:
    entries = [entry("cunt"), entry("scunthorpe", id=2, kind="allow")]
    assert censor(entries, "greetings from scunthorpe") == "greetings from scunthorpe"
    assert censor(entries, "what a cunt") == "what a ****"


@pytest.mark.parametrize(
    ("action", "extra", "expected"),
    [
        ("mask", {}, "you *** person"),
        ("replace", {"replacement": "flowers"}, "you flowers person"),
        ("tag", {"category": "slur"}, "you [slur] person"),
    ],
)
def test_actions(action: str, extra: dict[str, str], expected: str) -> None:
    assert censor([entry("bad", action=action, **extra)], "you bad person") == expected


def test_block_stops_the_message_entirely() -> None:
    result = ChannelFilter([entry("bad", action="block")]).apply("you bad person")
    assert result.blocked and result.patterns() == ["bad"]


def test_wildcards_and_regex_entries() -> None:
    assert censor([entry("bad*", kind="wildcard")], "you badwolf") == "you *******"
    assert censor([entry(r"\d{4,}", kind="regex", action="tag", category="digits")], "call 5551234") == (
        "call [digits]"
    )


def test_unusable_patterns_are_rejected_at_compile_time() -> None:
    with pytest.raises(FilterError, match="invalid regex"):
        compile_entry(entry("(unclosed", kind="regex"))
    with pytest.raises(FilterError, match="1–200"):
        compile_entry(entry(""))
    with pytest.raises(FilterError, match="normalizes to nothing"):
        compile_entry(entry("​"))


def test_a_bad_row_does_not_break_the_rest_of_the_list() -> None:
    entries = [entry("(unclosed", kind="regex"), entry("bad", id=2)]
    assert censor(entries, "that is bad") == "that is ***"


def test_disabled_entries_are_ignored() -> None:
    assert censor([entry("bad", enabled=False)], "that is bad") == "that is bad"


def test_several_hits_in_one_message() -> None:
    entries = [entry("bad"), entry("worse", id=2, action="replace", replacement="ok")]
    assert censor(entries, "bad and worse") == "*** and ok"


# ── the service, against the database ──────────────────────────────────────
@pytest.fixture
async def service(dbs: Databases) -> AsyncIterator[FilterService]:
    filters = FilterService(dbs.bot)
    await filters.reload()
    yield filters


async def test_entries_are_stored_audited_and_applied(service: FilterService, dbs: Databases) -> None:
    added = await service.add(channel_id=CHANNEL, pattern="bad", actor_user_id="300")
    assert service.apply(CHANNEL, "you bad person") == ("you *** person", ["bad"])

    async with await dbs.bot.execute("SELECT action, target FROM audit_log") as cur:
        assert [tuple(r.values()) for r in await cur.fetchall()] == [("filter.add", "bad")]

    assert await service.set_enabled(
        channel_id=CHANNEL, entry_id=added.id, enabled=False, actor_user_id="300"
    )
    assert service.apply(CHANNEL, "you bad person") == ("you bad person", [])
    assert await service.remove(channel_id=CHANNEL, entry_id=added.id, actor_user_id="300")
    assert service.entries_for(CHANNEL) == []


async def test_global_entries_apply_in_every_channel(service: FilterService) -> None:
    await service.add(channel_id=GLOBAL, pattern="bad", actor_user_id="1")
    assert service.apply("999", "so bad")[0] == "so ***"
    assert service.apply(CHANNEL, "so bad")[0] == "so ***"


async def test_rejects_reports_what_would_be_censored(service: FilterService) -> None:
    await service.add(channel_id=CHANNEL, pattern="bad", actor_user_id="300")
    assert service.rejects(CHANNEL, "a bad name") == ["bad"]
    assert service.rejects(CHANNEL, "a fine name") == []


# ── end to end: the bot's own output and what users store ──────────────────
async def test_the_filter_censors_what_the_bot_says(dbs: Databases) -> None:
    """Outbox path: the reply is censored, and the log keeps the text as it was before filtering."""
    from doomtp_bot.core.outbox import Outbox, SendResult

    sent: list[str] = []
    logged: list[dict[str, object]] = []

    class Sender:
        async def send_chat(self, channel_id: str, text: str, reply_to: str | None) -> SendResult:
            sent.append(text)
            return SendResult("t1")

    class Log:
        async def outbound(self, **kwargs: object) -> None:
            logged.append(kwargs)

    filters = FilterService(dbs.bot)
    await filters.reload()
    await filters.add(channel_id=CHANNEL, pattern="bad", actor_user_id="300")
    outbox = Outbox(Sender(), Log(), content_filter=filters.apply)

    await outbox.send(CHANNEL, "that was bad")
    assert sent == ["that was ***"]
    assert logged[0]["text_prefilter"] == "that was bad" and logged[0]["filter_hits"] == ["bad"]

    await filters.add(channel_id=CHANNEL, pattern="worse", action="block", actor_user_id="300")
    results = await outbox.send(CHANNEL, "this is worse")
    assert results[0].dropped_reason == "filter_block" and sent == ["that was ***"]
    assert logged[-1]["dropped_reason"] == "filter_block"


async def test_the_filter_rejects_stored_content(dbs: Databases) -> None:
    """Custom command bodies, names and variable values are checked at save time (architecture §9)."""
    import dataclasses

    from doomtp_bot.customcmds.packs import PackService
    from doomtp_bot.customcmds.resolution import CustomCommandLoader
    from doomtp_bot.customcmds.service import CustomCommandService
    from doomtp_bot.modules import builtin_registry
    from doomtp_bot.policy.repository import Actor
    from doomtp_bot.policy.service import PolicyService
    from doomtp_bot.runtime.engine import Runtime
    from doomtp_bot.variables.access import VariableAccessPolicy
    from doomtp_bot.variables.store import PostgresVariableStore

    policy = PolicyService(dbs.bot, clock=TickingClock())
    await policy.reload()
    await policy.mutate(lambda repo: repo.ensure_channel(CHANNEL, "doomtp", Actor(None, "system")))
    store = PostgresVariableStore(dbs.bot)
    access = VariableAccessPolicy(policy, dbs.bot)
    await access.reload()
    commands = CustomCommandService(dbs.bot, on_grants_changed=access.reload)
    packs = PackService(dbs.bot, commands)
    filters = FilterService(dbs.bot)
    await filters.reload()
    await filters.add(channel_id=CHANNEL, pattern="bad", actor_user_id="300")
    runtime = Runtime(
        builtin_registry(),
        policy=policy,
        store=store,
        access=access,
        custom=CustomCommandLoader(commands, packs),
        services={
            "policy": policy,
            "variable_store": store,
            "customcmds": commands,
            "packs": packs,
            "variable_access": access,
            "filters": filters,
        },
    )

    channel = dataclasses.replace(policy.channel_info(CHANNEL, "doomtp"), prefix="!")
    chatter = policy.build_chatter(CHANNEL, "400", "alice", "Alice")
    mod = policy.build_chatter(CHANNEL, "300", "mod", "Mod", frozenset({"moderator"}))

    async def run(text: str, who: object = None) -> tuple[int, str]:
        report = await runtime.run(text, runtime.make_context(channel=channel, invoker=who or chatter))
        assert report is not None
        return report.result.code, report.result.message or ""

    code, message = await run("!cc add greet echo you are bad")
    assert code == Code.USAGE and "filter rejects" in message  # the body
    code, _ = await run("!cc add bad echo hello")
    assert code == Code.USAGE  # the name

    assert (await run("!cc add greet echo hello there"))[0] == Code.OK
    code, message = await run("!var set chatter.note that was bad")
    assert code == Code.USAGE and "filter rejects" in message  # a stored value
    assert (await run("!var set chatter.note that was fine"))[0] == Code.OK

    # Everything else a custom command stores is read out later, so it is filtered too (ADR-0009 item 4).
    for stored in (
        "!cc describe greet a bad idea",
        '!cc param greet 1 name=who "a bad person"',
        "!cc pack create bad",
        "!cc pack create nice a bad set",
    ):
        code, message = await run(stored)
        assert (code, "filter rejects" in message) == (Code.USAGE, True), stored
    assert (await run("!cc add mine echo hi", mod))[0] == Code.OK
    assert (await run("!cc publish mine as bad", mod))[0] == Code.USAGE  # the published name
    assert (await run("!cc publish mine as hello", mod))[0] == Code.OK
