"""Triggers, listeners and timers (architecture §7)."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from doomtp_bot.core.events import ChatNotification
from doomtp_bot.core.outbox import Outbox, SendResult
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
from tests.customcmds.test_customcmds import TickingClock

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
class FakeClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


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
    assert reply is not None and "need channel authorization the bot lacks" in reply


async def test_triggers_run_at_the_creators_rank(h: Harness) -> None:
    await h.say("mod", r"!trigger listen \bhello\b => echo hi")
    trigger = h.triggers.in_channel(CHANNEL_ID)[0]
    assert trigger.run_as_rank == 80  # the moderator who created it, never higher


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
    """The whole path: a chat line and a raid notification reach their triggers."""
    from doomtp_bot.chatlog.writer import ChatLogWriter
    from doomtp_bot.core.channels import ChannelManager
    from doomtp_bot.core.dispatch import Dispatcher
    from doomtp_bot.core.events import ChatMessage
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
    sender = FakeSender()
    outbox = Outbox(sender, None)
    writer = ChatLogWriter(dbs.chatlog)
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
    await dispatcher.drain()
    await writer.stop()
    assert sorted(sender.sent) == ["heard hello", "raid!"]


# ── timers ─────────────────────────────────────────────────────────────────
async def timer_scheduler(h: Harness) -> TimerScheduler:
    return TimerScheduler(
        triggers=h.triggers, runner=h.runner, policy=h.policy, activity=h.activity, clock=h.clock
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
    scheduler = await timer_scheduler(h)
    h.clock.now = 120
    assert await scheduler.tick() == []  # stream status needs the poller (ADR-0007), so: not live


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
