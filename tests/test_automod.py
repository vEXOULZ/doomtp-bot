"""AutoMod-assisted filtering: incoming chat the word list would block (architecture §9.3)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from doomtp_bot.chatlog.writer import ChatLogWriter
from doomtp_bot.core.capabilities import MODERATE
from doomtp_bot.core.channels import ChannelManager
from doomtp_bot.core.dispatch import Dispatcher
from doomtp_bot.core.events import Badge, ChatMessage
from doomtp_bot.core.outbox import Outbox, SendResult
from doomtp_bot.filters.service import FilterService
from doomtp_bot.moderation.automod import AutoMod
from doomtp_bot.moderation.index import ModerationIndex
from doomtp_bot.modules import builtin_registry
from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.runtime.engine import Runtime
from doomtp_bot.runtime.result import Code
from doomtp_bot.storage.db import Databases

BOT_ID, BOT_LOGIN = "999", "doomtp_bot"
CHANNEL_ID, CHANNEL_LOGIN = "100", "doomtp"
USERS = {"alice": "400", "mod": "300", "doomtp": CHANNEL_ID, "owner": "1"}
REASON = "filtered by the channel's word list"


@dataclass
class FakeTwitch:
    """Chat, plus the two moderation calls AutoMod uses."""

    bot_id: str = BOT_ID
    bot_login: str = BOT_LOGIN
    sent: list[tuple[str, str, str | None]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    timeouts: list[tuple[str, int, str]] = field(default_factory=list)

    async def subscribe_channel(self, channel_id: str) -> list[str]:
        return []

    async def unsubscribe_channel(self, channel_id: str) -> None: ...

    async def send_chat(self, channel_id: str, text: str, reply_to: str | None) -> SendResult:
        self.sent.append((channel_id, text, reply_to))
        return SendResult(f"t{len(self.sent)}")

    async def delete_message(self, channel_id: str, message_id: str) -> bool:
        self.deleted.append(message_id)
        return True

    async def timeout_user(self, channel_id: str, user_id: str, seconds: int, reason: str) -> bool:
        self.timeouts.append((user_id, seconds, reason))
        return True

    async def resolve_user(self, login: str) -> dict[str, str] | None:
        uid = USERS.get(login.lower().lstrip("@"))
        return {"id": uid, "name": login.lower(), "display": login.title()} if uid else None


@dataclass
class Harness:
    policy: PolicyService
    filters: FilterService
    twitch: FakeTwitch
    dispatcher: Dispatcher
    runtime: Runtime
    writer: ChatLogWriter
    counter: int = 0

    async def say(self, login: str, text: str, *, badges: tuple[str, ...] = ()) -> str:
        self.counter += 1
        mid = f"m{self.counter}"
        await self.dispatcher.handle(
            ChatMessage(
                message_id=mid,
                channel_id=CHANNEL_ID,
                channel_login=CHANNEL_LOGIN,
                user_id=USERS.get(login, "500"),
                user_login=login,
                display_name=login.title(),
                text=text,
                sent_at=1_000_000 + self.counter,
                received_at=1_000_000 + self.counter,
                badges=tuple(Badge(b, "1") for b in badges),
            )  # fmt: skip
        )
        await self.dispatcher.drain()
        return mid

    async def run(self, login: str, text: str, badges: tuple[str, ...] = ()) -> Any:
        channel = self.policy.channel_info(CHANNEL_ID, CHANNEL_LOGIN)
        chatter = self.policy.build_chatter(
            CHANNEL_ID, USERS.get(login, "500"), login, login.title(), frozenset(badges)
        )
        return await self.runtime.run(text, self.runtime.make_context(channel=channel, invoker=chatter))

    async def set_automod(self, action: str, seconds: int | None = None) -> None:
        await self.policy.mutate(
            lambda r: r.set_channel_field(CHANNEL_ID, "automod_action", action, Actor(None, "test"))
        )
        if seconds is not None:
            await self.policy.mutate(
                lambda r: r.set_channel_field(CHANNEL_ID, "automod_timeout_s", seconds, Actor(None, "test"))
            )

    async def moderate_capability(self, granted: bool) -> None:
        capabilities = {"chat", MODERATE} if granted else {"chat"}
        await self.policy.mutate(
            lambda r: r.set_channel_field(CHANNEL_ID, "capabilities", capabilities, Actor(None, "test"))
        )


@pytest.fixture
async def h(dbs: Databases) -> AsyncIterator[Harness]:
    policy = PolicyService(dbs.bot, bot_owner_ids=frozenset({"1"}))
    await policy.reload()
    writer = ChatLogWriter(dbs.chatlog, flush_interval=0.01)
    writer.start()
    twitch = FakeTwitch()
    filters = FilterService(dbs.bot)
    await filters.reload()
    runtime = Runtime(
        builtin_registry(),
        policy=policy,
        callbacks=policy,
        resolve_user=twitch.resolve_user,
        services={"policy": policy, "twitch": twitch, "filters": filters},
    )
    channels = ChannelManager(policy, twitch, writer, default_prefix="!")
    runtime.services["channels"] = channels
    dispatcher = Dispatcher(
        runtime=runtime,
        policy=policy,
        writer=writer,
        outbox=Outbox(twitch, writer),
        moderation=ModerationIndex(),
        channels=channels,
        automod=AutoMod(policy=policy, filters=filters, moderator=twitch),
    )
    await channels.ensure_home(BOT_ID, BOT_LOGIN)
    await channels.join(CHANNEL_ID, CHANNEL_LOGIN, Actor(None, "system"))
    await policy.mutate(lambda r: r.set_channel_field(CHANNEL_ID, "prefix", "!", Actor(None, "test")))
    await filters.add(
        channel_id=CHANNEL_ID, pattern="slur", kind="word", action="block", actor_user_id=None, via="chat"
    )
    await filters.add(
        channel_id=CHANNEL_ID, pattern="darn", kind="word", action="mask", actor_user_id=None, via="chat"
    )
    harness = Harness(policy, filters, twitch, dispatcher, runtime, writer)
    await harness.moderate_capability(True)
    try:
        yield harness
    finally:
        await dispatcher.drain()
        await writer.stop()


# ── enforcement ────────────────────────────────────────────────────────────
async def test_automod_is_off_until_a_channel_asks_for_it(h: Harness) -> None:
    await h.say("alice", "you are a slur")
    assert h.twitch.deleted == []


async def test_a_blocked_message_is_deleted(h: Harness) -> None:
    await h.set_automod("delete")
    mid = await h.say("alice", "you are a s.l.u.r")  # evasion is normalized away by the matcher
    assert h.twitch.deleted == [mid]
    assert h.twitch.timeouts == []


async def test_timeout_mode_deletes_and_times_the_chatter_out(h: Harness) -> None:
    await h.set_automod("timeout", 300)
    mid = await h.say("alice", "slur")
    assert h.twitch.deleted == [mid]
    assert h.twitch.timeouts == [("400", 300, REASON)]


async def test_only_block_entries_count(h: Harness) -> None:
    """mask/replace/tag rewrite what the bot says; they are not a judgement about the chatter."""
    await h.set_automod("delete")
    await h.say("alice", "well darn")
    assert h.twitch.deleted == []


async def test_moderators_and_the_broadcaster_are_never_actioned(h: Harness) -> None:
    await h.set_automod("timeout", 60)
    await h.say("mod", "the word slur is what we're filtering", badges=("moderator",))
    await h.say("doomtp", "slur", badges=("broadcaster",))
    assert (h.twitch.deleted, h.twitch.timeouts) == ([], [])


async def test_nothing_happens_without_the_moderator_tier(h: Harness) -> None:
    await h.set_automod("delete")
    await h.moderate_capability(False)
    await h.say("alice", "slur")
    assert h.twitch.deleted == []


async def test_a_deleted_message_does_not_run_its_command(h: Harness) -> None:
    await h.set_automod("delete")
    mid = await h.say("alice", "!echo slur")
    assert h.twitch.deleted == [mid]
    assert h.twitch.sent == []


# ── the !automod command ───────────────────────────────────────────────────
async def test_the_command_reports_and_changes_the_setting(h: Harness) -> None:
    report = await h.run("mod", "!automod", ("moderator",))
    assert report is not None and "automod is off" in (report.result.message or "")

    set_ = await h.run("mod", "!automod timeout 120", ("moderator",))
    assert set_ is not None and "times the chatter out for 120s" in (set_.result.message or "")
    settings = h.policy.channel_settings(CHANNEL_ID)
    assert settings is not None
    assert (settings.automod_action, settings.automod_timeout_s) == ("timeout", 120)

    off = await h.run("mod", "!automod off", ("moderator",))
    assert off is not None and "automod is off" in (off.result.message or "")


async def test_the_command_tests_text_and_needs_a_moderator(h: Harness) -> None:
    await h.set_automod("delete")
    tried = await h.run("mod", "!automod test that is a slur", ("moderator",))
    assert tried is not None and "automod would delete it" in (tried.result.message or "")
    clean = await h.run("mod", "!automod test hello", ("moderator",))
    assert clean is not None and "leave that alone" in (clean.result.message or "")

    viewer = await h.run("alice", "!automod delete")
    assert viewer is not None and viewer.result.code == Code.DENIED


async def test_the_command_is_unavailable_without_the_moderator_tier(h: Harness) -> None:
    """An unmet `requires` reads as an unknown command, so it never advertises what it can't do."""
    await h.moderate_capability(False)
    report = await h.run("mod", "!automod delete", ("moderator",))
    assert report is not None and report.result.code == Code.UNKNOWN
