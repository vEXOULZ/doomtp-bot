"""Triggers owned by a pack (ADR-0029): any type, firing wherever the pack is published and its module is on."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from doomtp_bot.core.outbox import Outbox, SendResult
from doomtp_bot.customcmds.packs import Pack, PackService
from doomtp_bot.customcmds.service import CustomCommandService
from doomtp_bot.filters.service import FilterService
from doomtp_bot.modules import builtin_registry
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.runtime.engine import Runtime
from doomtp_bot.storage.db import Databases
from doomtp_bot.triggers.runner import TriggerRunner
from doomtp_bot.triggers.service import PackTriggerError, TriggerError, TriggerService
from doomtp_bot.triggers.timers import ChatActivity, TimerScheduler
from tests.fakes import SETUP, FakeClock, TickingClock, policy_with_channels

A, B = ("100", "doomtp"), ("101", "other")
OWNER = "900"


@dataclass
class Sender:
    sent: list[tuple[str, str]] = field(default_factory=list)

    async def send_chat(self, channel_id: str, text: str, reply_to: str | None) -> SendResult:
        self.sent.append((channel_id, text))
        return SendResult(f"t{len(self.sent)}")


@dataclass
class Harness:
    policy: PolicyService
    packs: PackService
    pack: Pack
    triggers: TriggerService
    runtime: Runtime
    scheduler: TimerScheduler
    sender: Sender
    clock: FakeClock

    async def say(self, channel: tuple[str, str], text: str) -> str | None:
        """A moderator in `channel` types `text`."""
        info = dataclasses.replace(self.policy.channel_info(*channel), prefix="!")
        chatter = self.policy.build_chatter(channel[0], "300", "mod", "Mod", frozenset({"moderator"}))
        report = await self.runtime.run(text, self.runtime.make_context(channel=info, invoker=chatter))
        assert report is not None
        return report.send

    async def module(self, channel_id: str, enabled: bool | None) -> None:
        await self.policy.mutate(lambda repo: repo.set_module_toggle(channel_id, "chimes", enabled, SETUP))


@pytest.fixture
async def h(dbs: Databases) -> AsyncIterator[Harness]:
    policy = await policy_with_channels(dbs.bot, A, B, joined=True, clock=TickingClock())
    filters = FilterService(dbs.bot)
    await filters.reload()
    packs = PackService(dbs.bot, CustomCommandService(dbs.bot, filters=filters))
    pack = await packs.create(owner_user_id=OWNER, name="chimes", actor_via="script")
    triggers = TriggerService(dbs.bot, filters=filters, scope=policy)
    await triggers.reload()
    packs.on_published = triggers.reload
    sender = Sender()
    outbox = Outbox(sender, None)
    runtime = Runtime(builtin_registry(), policy=policy, services={"policy": policy, "triggers": triggers})
    triggers.parser_params = runtime.parser_params
    clock = FakeClock()
    scheduler = TimerScheduler(
        triggers=triggers,
        runner=TriggerRunner(runtime=runtime, policy=policy, outbox=outbox),
        policy=policy,
        activity=ChatActivity(),
        clock=clock,
    )
    yield Harness(policy, packs, pack, triggers, runtime, scheduler, sender, clock)


async def _hourly(h: Harness, expr: str = "echo the hour") -> None:
    assert await h.triggers.install_pack_trigger(
        pack_id=h.pack.id, key="hourly", type_="timer", expr=expr, schedule={"every_s": 3600},
        run_as_rank=80, created_by=OWNER,
    )  # fmt: skip


async def _publish(h: Harness, scope: str) -> None:
    await h.packs.publish(channel_id=scope, pack=h.pack, published_by=OWNER, actor_via="script")


async def test_an_hourly_pack_timer_runs_in_every_channel_with_the_pack(h: Harness) -> None:
    await _hourly(h)
    await _publish(h, GLOBAL)
    assert await h.scheduler.tick() == []
    h.clock.now = 3601
    fired = await h.scheduler.tick()
    assert sorted(t.channel_id for t in fired) == [A[0], B[0]]
    assert sorted(h.sender.sent) == [(A[0], "the hour"), (B[0], "the hour")]
    h.clock.now = 4000  # each channel keeps its own clock for it
    assert await h.scheduler.tick() == []


async def test_it_follows_the_module_toggle_and_the_publication(h: Harness) -> None:
    await _hourly(h)
    assert h.triggers.timers() == []  # not published anywhere yet
    await _publish(h, B[0])
    assert [t.channel_id for t in h.triggers.timers()] == [B[0]]
    await _publish(h, GLOBAL)
    await h.module(B[0], False)
    assert [t.channel_id for t in h.triggers.timers()] == [A[0]]
    await h.module(GLOBAL, False)
    assert h.triggers.timers() == []
    await h.module(GLOBAL, None)
    await h.packs.unpublish(channel_id=GLOBAL, pack=h.pack, actor_user_id=OWNER)
    assert h.triggers.timers() == []  # B's own publication stands, but its module is still off


async def test_any_type_works_events_and_listeners_too(h: Harness) -> None:
    await _publish(h, GLOBAL)
    for key, type_, match in (("raid", "raid", {}), ("hello", "listener", {"regex": r"\bhello\b"})):
        await h.triggers.install_pack_trigger(
            pack_id=h.pack.id, key=key, type_=type_, expr="echo hi", match=match, run_as_rank=0, created_by=OWNER
        )
    [raid] = h.triggers.event_triggers(B[0], "raid", {"viewers": 3})
    assert (raid.channel_id, raid.pack) == (B[0], "chimes")
    [(listener, fields)] = h.triggers.listeners_matching(A[0], "well hello there")
    assert listener.channel_id == A[0] and fields["0"] == "hello"


async def test_install_is_checked_updates_in_place_and_drops_what_is_gone(h: Harness) -> None:
    with pytest.raises(TriggerError):
        await h.triggers.install_pack_trigger(
            pack_id=h.pack.id, key="bad", type_="timer", expr="echo x", run_as_rank=0, created_by=OWNER
        )
    await _hourly(h)
    assert not await h.triggers.install_pack_trigger(
        pack_id=h.pack.id, key="hourly", type_="timer", expr="echo the hour", schedule={"every_s": 3600},
        run_as_rank=80, created_by=OWNER,
    )  # fmt: skip
    first = h.triggers.pack_triggers("chimes")[0]
    await _hourly(h, "echo the new hour")
    [updated] = h.triggers.pack_triggers("chimes")
    assert (updated.id, updated.expr) == (first.id, "echo the new hour")
    assert await h.triggers.remove_pack_triggers(pack_id=h.pack.id, keep=[], actor_user_id=OWNER) == ["hourly"]
    assert h.triggers.pack_triggers("chimes") == []


async def test_a_channel_sees_a_pack_trigger_but_cannot_change_it(h: Harness) -> None:
    await _hourly(h)
    await _publish(h, GLOBAL)
    trigger = h.triggers.pack_triggers("chimes")[0]
    listing = await h.say(A, "!timer list")
    assert listing is not None and f"{trigger.id}:every 3600s (chimes pack)" in listing
    for action in ("off", "rm"):
        refused = await h.say(A, f"!timer {action} {trigger.id}")
        assert refused is not None and "belongs to the chimes pack" in refused
    with pytest.raises(PackTriggerError):
        await h.triggers.update(
            channel_id=A[0], trigger_id=trigger.id, expr="echo mine", actor_user_id="300", prefix="!", via="api"
        )
    assert h.triggers.pack_triggers("chimes")[0].enabled
