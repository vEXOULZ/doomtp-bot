"""Triggers, listeners and timers (architecture §7)."""

from __future__ import annotations

import dataclasses
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from doomtp_bot.core.events import ChatNotification
from doomtp_bot.core.outbox import Outbox, SendResult
from doomtp_bot.core.streams import StreamStatus
from doomtp_bot.modules import builtin_registry
from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.runtime.engine import Runtime
from doomtp_bot.runtime.result import Code
from doomtp_bot.runtime.spec import LogLevel
from doomtp_bot.storage.db import Databases
from doomtp_bot.triggers.runner import TriggerRunner
from doomtp_bot.triggers.service import (
    TriggerError,
    TriggerService,
    compile_listener,
    match_fields,
    parse_every,
)
from doomtp_bot.triggers.timers import ChatActivity, TimerScheduler
from tests.fakes import FakeClock, TickingClock

CHANNEL_ID, CHANNEL_LOGIN = "100", "doomtp"
USERS = {"mod": ("300", "mod", "Mod"), "alice": ("400", "alice", "Alice")}
BADGES = {"mod": {"moderator"}}


@dataclass
class FakeSender:
    sent: list[str] = field(default_factory=list)

    async def send_chat(self, channel_id: str, text: str, reply_to: str | None) -> SendResult:
        self.sent.append(text)
        return SendResult(f"t{len(self.sent)}")


@dataclass
class Harness:
    policy: PolicyService
    triggers: TriggerService
    runner: TriggerRunner
    runtime: Runtime
    sender: FakeSender
    activity: ChatActivity
    clock: FakeClock

    async def say(self, who: str, text: str) -> str | None:
        """Run a typed command, e.g. !trigger add …"""
        user = USERS[who]
        channel = dataclasses.replace(self.policy.channel_info(CHANNEL_ID, CHANNEL_LOGIN), prefix="!")
        chatter = self.policy.build_chatter(
            CHANNEL_ID, user[0], user[1], user[2], frozenset(BADGES.get(who, set()))
        )
        report = await self.runtime.run(text, self.runtime.make_context(channel=channel, invoker=chatter))
        assert report is not None
        return report.send


@pytest.fixture
async def h(dbs: Databases) -> AsyncIterator[Harness]:
    policy = PolicyService(dbs.bot, clock=TickingClock())
    await policy.reload()
    await policy.mutate(lambda repo: repo.ensure_channel(CHANNEL_ID, CHANNEL_LOGIN, Actor(None, "system")))
    triggers = TriggerService(dbs.bot)
    await triggers.reload()
    sender = FakeSender()
    outbox = Outbox(sender, None)
    runtime = Runtime(builtin_registry(), policy=policy, services={"policy": policy, "triggers": triggers})
    runner = TriggerRunner(runtime=runtime, policy=policy, outbox=outbox)
    yield Harness(policy, triggers, runner, runtime, sender, ChatActivity(), FakeClock())


# ── pieces ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("text", "seconds"), [("90s", 90), ("15m", 900), ("2h", 7200), ("300", 300)])
def test_interval_parsing(text: str, seconds: int) -> None:
    assert parse_every(text) == seconds


@pytest.mark.parametrize("text", ["", "10s", "soon", "48h"])
def test_impossible_intervals_are_refused(text: str) -> None:
    with pytest.raises(TriggerError):
        parse_every(text)


def test_listener_captures_become_match_fields() -> None:
    pattern = compile_listener(r"my name is (?P<name>\w+)")
    assert match_fields(pattern, "hi, my name is alice") == {
        "0": "my name is alice",
        "1": "alice",
        "name": "alice",
    }
    assert match_fields(pattern, "nothing here") is None


def test_listener_patterns_are_limited() -> None:
    with pytest.raises(TriggerError, match="1–200"):
        compile_listener("x" * 201)
    with pytest.raises(TriggerError, match="invalid regex"):
        compile_listener("(unclosed")


def test_a_catastrophic_listener_gives_up_instead_of_stalling() -> None:
    # Under `re` this takes seconds, and hours a few characters later, with the whole bot stalled meanwhile.
    pattern = compile_listener("(a|aa)+$")
    started = time.perf_counter()
    assert match_fields(pattern, "a" * 34 + "!") is None
    assert time.perf_counter() - started < 1


# ── managing them from chat ────────────────────────────────────────────────
async def test_add_list_and_remove_a_listener(h: Harness) -> None:
    added = await h.say("mod", r"!trigger listen \bhello\b => echo hi {chatter.display}")
    assert added is not None and added.startswith("added listener trigger ")
    listing = await h.say("mod", "!trigger list")
    assert listing is not None and "hello" in listing and "echo hi" in listing

    trigger = h.triggers.in_channel(CHANNEL_ID)[0]
    assert await h.say("mod", f"!trigger off {trigger.id}") == f"{trigger.id} is off"
    assert h.triggers.listeners_matching(CHANNEL_ID, "hello there") == []
    assert await h.say("mod", f"!trigger on {trigger.id}") == f"{trigger.id} is on"
    assert await h.say("mod", f"!trigger rm {trigger.id}") == f"removed {trigger.id}"
    assert h.triggers.in_channel(CHANNEL_ID) == []


async def test_a_broken_expression_is_never_stored(h: Harness) -> None:
    refused = await h.say("mod", "!trigger listen hello => echo a ; b")
    assert refused is not None and "E_RESERVED_OPERATOR" in refused
    assert h.triggers.in_channel(CHANNEL_ID) == []


async def test_unsupported_types_are_stored_with_a_warning(h: Harness) -> None:
    reply = await h.say("mod", "!trigger add follow echo thanks for following")
    assert reply is not None and "grants followers" in reply  # stored, but the bot isn't a mod here


async def test_triggers_run_at_the_creators_rank(h: Harness) -> None:
    await h.say("mod", r"!trigger listen \bhello\b => echo hi")
    trigger = h.triggers.in_channel(CHANNEL_ID)[0]
    assert trigger.run_as_rank == 80  # the moderator who created it, never higher


async def test_a_trigger_made_by_a_trigger_never_outranks_it(h: Harness) -> None:
    # A moderator-rank listener set off by the broadcaster: what it creates gets the listener's rank, not the
    # broadcaster's, or a trigger could hand out more than it was ever given.
    await h.triggers.add(
        channel_id=CHANNEL_ID,
        type_="listener",
        expr="trigger add raid echo raid!",
        match={"regex": r"\bmake one\b"},
        run_as_rank=80,
        created_by="300",
    )
    [(listener, fields)] = h.triggers.listeners_matching(CHANNEL_ID, "make one")
    report = await h.runner.run(
        listener, channel_login=CHANNEL_LOGIN, match=fields, user=(CHANNEL_ID, CHANNEL_LOGIN, "DoomTP")
    )
    assert report is not None and report.result.ok, report and report.result.message
    assert [t.run_as_rank for t in h.triggers.in_channel(CHANNEL_ID) if t.type == "raid"] == [80]


# ── running them ───────────────────────────────────────────────────────────
async def test_a_listener_runs_with_its_captures(h: Harness) -> None:
    await h.say("mod", r"!trigger listen my name is (?P<name>\w+) => echo nice to meet you {match.name}")
    hits = h.triggers.listeners_matching(CHANNEL_ID, "hi, my name is alice")
    assert len(hits) == 1
    trigger, fields = hits[0]
    await h.runner.run(
        trigger,
        channel_login=CHANNEL_LOGIN,
        match=fields,
        user=USERS["alice"],
        input_text="hi, my name is alice",
    )
    assert h.sender.sent == ["nice to meet you alice"]


async def test_an_event_trigger_reads_the_payload(h: Harness) -> None:
    await h.say("mod", "!trigger add raid echo welcome {event.user.name} with {event.viewers} raiders")
    payload = {"user": {"id": "500", "name": "raider"}, "viewers": 42}
    found = h.triggers.event_triggers(CHANNEL_ID, "raid", payload)
    assert len(found) == 1
    await h.runner.run(found[0], channel_login=CHANNEL_LOGIN, event=payload, user=("500", "raider", "Raider"))
    assert h.sender.sent == ["welcome raider with 42 raiders"]


async def test_match_conditions_filter_events(h: Harness) -> None:
    await h.triggers.add(
        channel_id=CHANNEL_ID,
        type_="raid",
        expr="echo big raid",
        match={"min_viewers": 50},
        run_as_rank=80,
        created_by="300",
    )
    assert h.triggers.event_triggers(CHANNEL_ID, "raid", {"viewers": 10}) == []
    assert len(h.triggers.event_triggers(CHANNEL_ID, "raid", {"viewers": 80})) == 1


async def test_the_dispatcher_runs_listeners_and_notification_triggers(dbs: Databases) -> None:
    """The whole path: a chat line, a raid notification and a stream going live reach their triggers."""
    from doomtp_bot.chatlog.writer import ChatLogWriter
    from doomtp_bot.core.channels import ChannelManager
    from doomtp_bot.core.dispatch import Dispatcher
    from doomtp_bot.core.events import ChatMessage, StreamStatusChanged
    from doomtp_bot.moderation.index import ModerationIndex

    policy = PolicyService(dbs.bot, clock=TickingClock())
    await policy.reload()
    await policy.mutate(lambda repo: repo.ensure_channel(CHANNEL_ID, CHANNEL_LOGIN, Actor(None, "system")))
    await policy.mutate(lambda repo: repo.set_channel_field(CHANNEL_ID, "status", "joined", Actor(None, "s")))
    triggers = TriggerService(dbs.bot)
    await triggers.add(
        channel_id=CHANNEL_ID,
        type_="listener",
        expr="echo heard {match.0}",
        match={"regex": r"\bhello\b"},
        run_as_rank=0,
        created_by="300",
    )
    await triggers.add(
        channel_id=CHANNEL_ID, type_="raid", expr="echo raid!", run_as_rank=0, created_by="300"
    )
    await triggers.add(
        channel_id=CHANNEL_ID,
        type_="redemption",
        expr="echo {event.user.display} wants {event.reward.title}: {event.input}",
        match={"reward_id": "rw1"},
        run_as_rank=0,
        created_by="300",
    )
    await triggers.add(
        channel_id=CHANNEL_ID,
        type_="stream_online",
        expr="echo live: {event.title}",
        run_as_rank=0,
        created_by="300",
    )
    sender = FakeSender()
    outbox = Outbox(sender, None)
    writer = ChatLogWriter(dbs.chatlog)
    streams = StreamStatus()
    runtime = Runtime(builtin_registry(), policy=policy, services={"policy": policy})
    channels = ChannelManager(policy, None, writer)
    dispatcher = Dispatcher(
        runtime=runtime,
        policy=policy,
        writer=writer,
        outbox=outbox,
        moderation=ModerationIndex(),
        channels=channels,
        triggers=triggers,
        trigger_runner=TriggerRunner(runtime=runtime, policy=policy, outbox=outbox),
        activity=ChatActivity(),
        streams=streams,
    )

    await dispatcher.handle(
        ChatMessage(
            message_id="m1",
            channel_id=CHANNEL_ID,
            channel_login=CHANNEL_LOGIN,
            user_id="400",
            user_login="alice",
            display_name="Alice",
            text="well hello there",
            sent_at=1,
            received_at=1,
        )  # fmt: skip
    )
    await dispatcher.handle(
        ChatNotification("n1", CHANNEL_ID, "500", "raid", {"user": {"name": "raider"}}, sent_at=2)
    )
    streams.streams[CHANNEL_ID] = {"title": "bot night"}  # what the poller just saw
    await dispatcher.handle(StreamStatusChanged(CHANNEL_ID, True, at=3))
    # A channel point redemption, once the broadcaster has connected (ADR-0007 item 5), plus one for a
    # reward this trigger isn't watching.
    redeemed = {
        "user": {"id": "400", "name": "alice", "display": "Alice"},
        "input": "a song",
        "reward": {"id": "rw1", "title": "Song request", "cost": 500},
    }
    await dispatcher.handle(ChatNotification("n2", CHANNEL_ID, "400", "redemption", redeemed, sent_at=4))
    await dispatcher.handle(
        ChatNotification(
            "n3",
            CHANNEL_ID,
            "400",
            "redemption",
            {**redeemed, "reward": {"id": "rw2", "title": "Hydrate", "cost": 50}},
            sent_at=5,
        )
    )
    await dispatcher.drain()
    await writer.stop()
    assert sorted(sender.sent) == [
        "Alice wants Song request: a song",
        "heard hello",
        "live: bot night",
        "raid!",
    ]


# ── timers ─────────────────────────────────────────────────────────────────
async def timer_scheduler(
    h: Harness, *, streams: StreamStatus | None = None, wall: str = "2026-09-18T18:00"
) -> TimerScheduler:
    moment = datetime.fromisoformat(wall).replace(tzinfo=UTC)
    return TimerScheduler(
        triggers=h.triggers,
        runner=h.runner,
        policy=h.policy,
        activity=h.activity,
        clock=h.clock,
        streams=streams,
        wall=lambda: moment,
    )


async def test_a_timer_fires_on_its_interval(h: Harness) -> None:
    await h.say("mod", "!timer add 15m echo remember to hydrate")
    scheduler = await timer_scheduler(h)

    assert await scheduler.tick() == []  # nothing is due at the first tick
    h.clock.now = 899
    assert await scheduler.tick() == []
    h.clock.now = 901
    assert len(await scheduler.tick()) == 1
    assert h.sender.sent == ["remember to hydrate"]

    h.clock.now = 1000  # too soon for the next one
    assert await scheduler.tick() == []
    h.clock.now = 1900
    assert len(await scheduler.tick()) == 1


async def test_a_timer_can_require_recent_chat(h: Harness) -> None:
    await h.say("mod", "!timer add 60s min_lines=3 echo still here")
    scheduler = await timer_scheduler(h)
    h.clock.now = 120

    assert await scheduler.tick() == []  # a quiet channel is left alone
    for _ in range(3):
        h.activity.saw_message(CHANNEL_ID)
    h.clock.now = 200
    assert len(await scheduler.tick()) == 1
    assert h.sender.sent == ["still here"]


async def test_a_timer_can_require_the_stream_to_be_live(h: Harness) -> None:
    await h.say("mod", "!timer add 60s only_live echo live only")
    streams = StreamStatus()
    scheduler = await timer_scheduler(h, streams=streams)
    h.clock.now = 120
    assert await scheduler.tick() == []  # offline: the channel is left alone

    streams.streams[CHANNEL_ID] = {"title": "live now"}
    h.clock.now = 200
    assert len(await scheduler.tick()) == 1
    assert h.sender.sent == ["live only"]


# ── crons ──────────────────────────────────────────────────────────────────
async def test_a_cron_fires_at_the_minute_it_names(h: Harness) -> None:
    reply = await h.say("mod", "!timer cron 0 18 * * fri => echo the stream starts now")
    assert reply is not None and "fri at 18:00" in reply and "UTC" in reply

    early = await timer_scheduler(h, wall="2026-09-18T17:59")
    assert await early.tick() == []

    on_time = await timer_scheduler(h, wall="2026-09-18T18:00")
    assert len(await on_time.tick()) == 1
    assert await on_time.tick() == []  # the same minute never fires twice
    assert h.sender.sent == ["the stream starts now"]

    wrong_day = await timer_scheduler(h, wall="2026-09-19T18:00")
    assert await wrong_day.tick() == []


async def test_a_cron_uses_the_channels_timezone(h: Harness) -> None:
    await h.policy.mutate(
        lambda repo: repo.set_channel_field(CHANNEL_ID, "timezone", "America/Sao_Paulo", Actor(None, "x"))
    )
    await h.say("mod", "!timer cron 0 18 * * * => echo boa noite")

    # 18:00 in São Paulo is 21:00 UTC, so the UTC evening is still the local afternoon.
    afternoon = await timer_scheduler(h, wall="2026-09-18T18:00")
    assert await afternoon.tick() == []
    evening = await timer_scheduler(h, wall="2026-09-18T21:00")
    assert len(await evening.tick()) == 1


async def test_a_bad_cron_is_refused_with_a_usable_message(h: Harness) -> None:
    reply = await h.say("mod", "!timer cron 0 18 * * => echo nope")
    assert reply is not None and "5 fields" in reply
    assert await h.say("mod", "!timer cron 0 18 * * xyz => echo nope") is not None
    assert h.triggers.crons() == []


async def test_crons_are_listed_with_the_timers(h: Harness) -> None:
    await h.say("mod", "!timer cron 30 9 * * mon-fri => echo good morning")
    listing = await h.say("mod", "!timer list")
    assert listing is not None and "good morning" in listing


async def test_timers_are_listed_and_removed_separately_from_triggers(h: Harness) -> None:
    await h.say("mod", "!timer add 60s echo tick")
    await h.say("mod", r"!trigger listen \bhi\b => echo hello")
    timers = await h.say("mod", "!timer list")
    assert timers is not None and "every 60s" in timers and "hello" not in timers
    listeners = await h.say("mod", "!trigger list")
    assert listeners is not None and "hi" in listeners and "tick" not in listeners


async def test_trigger_log_level_defaults_to_output(h: Harness) -> None:
    await h.say("mod", "!timer add 60s echo tick")
    assert h.triggers.timers()[0].log_level is LogLevel.OUTPUT


async def test_trigger_commands_need_a_moderator(h: Harness) -> None:
    channel = dataclasses.replace(h.policy.channel_info(CHANNEL_ID, CHANNEL_LOGIN), prefix="!")
    viewer = h.policy.build_chatter(CHANNEL_ID, *USERS["alice"])
    report = await h.runtime.run(
        "!timer add 60s echo nope", h.runtime.make_context(channel=channel, invoker=viewer)
    )
    assert report is not None and report.result.code == Code.DENIED and report.send is None
