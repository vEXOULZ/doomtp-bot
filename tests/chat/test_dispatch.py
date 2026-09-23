"""End-to-end without Twitch: chat events → log → runtime → outbox, plus !join/!part."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from doomtp_bot.chatlog.writer import ChatLogWriter
from doomtp_bot.core.channels import ChannelManager
from doomtp_bot.core.dispatch import Dispatcher
from doomtp_bot.core.events import Badge, ChatCleared, ChatMessage, ChatNotification, MessageDeleted
from doomtp_bot.core.outbox import BANNED, Outbox, SendResult
from doomtp_bot.core.streams import StreamStatus
from doomtp_bot.customcmds.resolution import CustomCommandLoader
from doomtp_bot.customcmds.service import CustomCommandService
from doomtp_bot.lang.parser import DEFAULT_PREFIX
from doomtp_bot.moderation.index import ModerationIndex
from doomtp_bot.modules import builtin_registry
from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.engine import Runtime
from doomtp_bot.runtime.registry import command
from doomtp_bot.runtime.result import Result
from doomtp_bot.runtime.spec import CommandSpec
from doomtp_bot.storage.db import Databases
from tests.fakes import policy_with_channels

BOT_ID, BOT_LOGIN = "999", "doomtp_bot"
CHANNEL_ID, CHANNEL_LOGIN = "100", "doomtp"
USERS = {"alice": "400", "bob": "401", "doomtp": CHANNEL_ID, "other": "500", "owner": "1"}


@dataclass
class FakeTwitch:
    bot_id: str = BOT_ID
    bot_login: str = BOT_LOGIN
    sent: list[tuple[str, str, str | None]] = field(default_factory=list)
    subscribed: list[str] = field(default_factory=list)
    refuse: bool = False
    banned_in: set[str] = field(default_factory=set)  # channels whose sends Twitch answers with 403

    async def subscribe_channel(self, channel_id: str) -> list[str]:
        if self.refuse:
            return ["channel.chat.message"]
        if channel_id not in self.subscribed:  # idempotent, like TwitchService
            self.subscribed.append(channel_id)
        return []

    async def unsubscribe_channel(self, channel_id: str) -> None:
        self.subscribed.remove(channel_id)

    async def send_chat(self, channel_id: str, text: str, reply_to: str | None) -> SendResult:
        if channel_id in self.banned_in:
            return SendResult(None, BANNED)
        self.sent.append((channel_id, text, reply_to))
        return SendResult(f"t{len(self.sent)}")

    async def resolve_user(self, login: str) -> dict[str, str] | None:
        uid = USERS.get(login.lower().lstrip("@"))
        return {"id": uid, "name": login.lower(), "display": login.title()} if uid else None


gate = asyncio.Event()


@command(CommandSpec(name="slowreply", module="test", summary="waits for the test to release it"))
async def slowreply(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    await gate.wait()
    return Result.success("finally")


@dataclass
class Harness:
    dbs: Databases
    policy: PolicyService
    writer: ChatLogWriter
    twitch: FakeTwitch
    dispatcher: Dispatcher
    channels: ChannelManager
    commands: CustomCommandService
    streams: StreamStatus
    counter: int = 0

    async def say(
        self, login: str, text: str, *, channel: str = CHANNEL_ID, badges: tuple[str, ...] = (), **kw: Any
    ) -> str:
        self.counter += 1
        mid = f"m{self.counter}"
        event = ChatMessage(
            message_id=mid, channel_id=channel, channel_login="doomtp" if channel == CHANNEL_ID else BOT_LOGIN,
            user_id=USERS.get(login, BOT_ID), user_login=login, display_name=login.title(), text=text,
            sent_at=1_000_000 + self.counter, received_at=1_000_000 + self.counter,
            badges=tuple(Badge(b, "1") for b in badges), **kw,
        )  # fmt: skip
        await self.dispatcher.handle(event)
        return mid

    async def settle(self) -> None:
        await self.dispatcher.drain()
        await self.writer.stop()
        self.writer.start()

    async def rows(self, sql: str) -> list[tuple[object, ...]]:
        async with await self.dbs.chatlog.execute(sql) as cur:
            return [tuple(r.values()) for r in await cur.fetchall()]


@pytest.fixture
async def h(dbs: Databases) -> AsyncIterator[Harness]:
    gate.clear()
    policy = await policy_with_channels(dbs.bot, bot_owner_ids=frozenset({"1"}))
    writer = ChatLogWriter(dbs.chatlog, flush_interval=0.01)
    writer.start()
    twitch = FakeTwitch()
    registry = builtin_registry()
    registry.add(slowreply)
    commands = CustomCommandService(dbs.bot)
    runtime = Runtime(
        registry,
        policy=policy,
        callbacks=policy,
        resolve_user=twitch.resolve_user,
        custom=CustomCommandLoader(commands),
        services={
            "policy": policy,
            "twitch": twitch,
            "customcmds": commands,
            "history": SimpleNamespace(base_url="https://history.example/api"),
        },
    )
    channels = ChannelManager(policy, twitch, writer, default_prefix="!")  # emoji default: see below
    runtime.services["channels"] = channels
    moderation = ModerationIndex()
    outbox = Outbox(twitch, writer, on_banned=channels.leave_banned)
    streams = StreamStatus()
    dispatcher = Dispatcher(
        runtime=runtime, policy=policy, writer=writer, outbox=outbox, moderation=moderation,
        channels=channels, streams=streams, customcmds=commands,
    )  # fmt: skip
    await channels.ensure_home(BOT_ID, BOT_LOGIN)
    await channels.subscribe_all()
    await channels.join(CHANNEL_ID, CHANNEL_LOGIN, Actor(None, "system"))
    try:
        yield Harness(dbs, policy, writer, twitch, dispatcher, channels, commands, streams)
    finally:
        gate.set()
        await dispatcher.drain()
        await writer.stop()


async def test_command_reply_is_sent_threaded_and_everything_logged(h: Harness) -> None:
    mid = await h.say("alice", "!random 1-1 | echo rolled {1}")
    await h.say("alice", "just chatting")
    await h.settle()
    assert h.twitch.sent == [(CHANNEL_ID, "rolled 1", mid)]
    assert await h.rows("SELECT text, is_command FROM messages ORDER BY sent_at") == [
        ("!random 1-1 | echo rolled {1}", 1),
        ("just chatting", 0),
    ]
    assert await h.rows("SELECT expr, code FROM command_runs") == [("!random 1-1 | echo rolled {1}", 0)]
    assert await h.rows("SELECT text_sent FROM outbound_msgs") == [("rolled 1",)]


async def test_ignored_users_bots_and_self_are_logged_but_never_run(h: Harness) -> None:
    await h.policy.mutate(lambda r: r.set_ignored(CHANNEL_ID, "401", "bob", True, Actor("1")))
    await h.say("bob", "!ping")
    await h.say("other", "!ping", badges=("bot-badge",))
    await h.say(BOT_LOGIN, "!ping", is_self=True)
    await h.settle()
    assert h.twitch.sent == []
    assert len(await h.rows("SELECT 1 FROM messages")) == 3


async def test_unjoined_channels_are_ignored_entirely(h: Harness) -> None:
    await h.say("alice", "!ping", channel="777")
    await h.settle()
    assert h.twitch.sent == [] and await h.rows("SELECT 1 FROM messages") == []


async def test_deleted_while_running_means_no_reply(h: Harness) -> None:
    mid = await h.say("alice", "!slowreply")
    await asyncio.sleep(0.01)
    await h.dispatcher.handle(MessageDeleted(CHANNEL_ID, mid, "400", at=1_000_100))
    gate.set()
    await h.settle()
    assert h.twitch.sent == []
    assert await h.rows("SELECT deleted_at IS NOT NULL FROM messages WHERE message_id = 'm1'") == [(1,)]


async def test_chat_clear_before_run_cancels(h: Harness) -> None:
    await h.dispatcher.handle(ChatCleared(CHANNEL_ID, at=2_000_000))
    await h.say("alice", "!ping")  # sent_at 1_000_001 < clear time: treated as cleared
    await h.settle()
    assert h.twitch.sent == []


async def test_notifications_logged(h: Harness) -> None:
    await h.dispatcher.handle(ChatNotification("n1", CHANNEL_ID, "500", "raid", {"viewers": 42}, sent_at=5))
    await h.settle()
    assert await h.rows("SELECT type FROM chat_notifications") == [("raid",)]


async def test_join_and_part_flow(h: Harness) -> None:
    await h.say("other", "!join", channel=BOT_ID)  # "other" types !join in the bot's own channel
    await h.settle()
    assert h.twitch.sent[-1][1].startswith("joined #other") and "500" in h.twitch.subscribed
    assert h.channels.is_active("500")

    await h.say("alice", "!join")  # outside the bot's channel
    await h.settle()
    assert "type !join in #doomtp_bot's chat" in h.twitch.sent[-1][1]

    await h.say("alice", "!part")  # not the broadcaster
    await h.settle()
    assert h.twitch.sent[-1][1] == "only the broadcaster can remove the bot"

    await h.say("owner", "!part other", channel=BOT_ID)
    await h.settle()
    assert h.twitch.sent[-1][1] == "bye! leaving #other" and not h.channels.is_active("500")
    assert await h.rows("SELECT end_reason FROM log_sessions WHERE channel_id = '500'") == [("part",)]


async def test_part_unsubscribes_so_join_works_again(h: Harness) -> None:
    await h.say("owner", "!part doomtp", channel=BOT_ID)
    await h.settle()
    assert CHANNEL_ID not in h.twitch.subscribed
    await h.say("owner", "!join doomtp", channel=BOT_ID)
    await h.settle()
    assert h.twitch.sent[-1][1].startswith("joined #doomtp") and CHANNEL_ID in h.twitch.subscribed
    assert await h.rows(
        f"SELECT end_reason FROM log_sessions WHERE channel_id = '{CHANNEL_ID}' ORDER BY id"
    ) == [
        ("part",),
        (None,),
    ]


async def test_a_403_on_a_send_leaves_the_channel_and_flags_it(h: Harness) -> None:
    h.twitch.banned_in.add(CHANNEL_ID)
    await h.say("alice", "!ping")
    await h.settle()
    settings = h.policy.channel_settings(CHANNEL_ID)
    assert settings is not None and settings.status == "banned" and not settings.active
    assert CHANNEL_ID not in h.twitch.subscribed
    assert await h.rows(f"SELECT end_reason FROM log_sessions WHERE channel_id = '{CHANNEL_ID}'") == [
        ("part",)
    ]
    assert await h.rows("SELECT dropped_reason FROM outbound_msgs") == [(BANNED,)]
    async with await h.dbs.bot.execute(
        "SELECT actor_user_id, via, after FROM audit_log WHERE action = 'channel.set.status'"
        " ORDER BY id DESC LIMIT 1"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None and row["actor_user_id"] is None and row["via"] == "system"
    assert "banned" in str(row["after"])

    await h.channels.leave_banned(CHANNEL_ID)  # a second refusal from a run in flight: nothing more
    assert h.policy.channel_settings(CHANNEL_ID).status == "banned"  # type: ignore[union-attr]


async def test_coming_back_after_a_ban_is_deliberate(h: Harness) -> None:
    await h.channels.leave_banned(CHANNEL_ID)
    h.twitch.banned_in.clear()

    await h.say("owner", "!join doomtp", channel=BOT_ID)
    await h.settle()
    assert "banned there" in h.twitch.sent[-1][1] and "!join doomtp rejoin" in h.twitch.sent[-1][1]
    assert not h.channels.is_active(CHANNEL_ID)

    await h.say("owner", "!join doomtp rejoin", channel=BOT_ID)
    await h.settle()
    assert h.twitch.sent[-1][1].startswith("joined #doomtp") and h.channels.is_active(CHANNEL_ID)


async def test_the_broadcaster_inviting_the_bot_back_is_deliberate_enough(h: Harness) -> None:
    await h.channels.leave_banned(CHANNEL_ID)
    await h.say("doomtp", "!join", channel=BOT_ID)
    await h.settle()
    assert h.twitch.sent[-1][1].startswith("joined #doomtp") and h.channels.is_active(CHANNEL_ID)


async def test_a_403_at_home_is_a_token_problem_not_a_ban(h: Harness) -> None:
    await h.channels.leave_banned(BOT_ID)
    assert h.channels.is_active(BOT_ID)


async def test_startup_subscribes_home_channel_once(h: Harness) -> None:
    await h.channels.ensure_home(BOT_ID, BOT_LOGIN)
    await h.channels.subscribe_all()
    assert sorted(h.twitch.subscribed) == sorted({BOT_ID, CHANNEL_ID})


async def test_join_reports_twitch_refusal(h: Harness) -> None:
    h.twitch.refuse = True
    await h.say("owner", "!join other", channel=BOT_ID)
    await h.settle()
    assert "Twitch refused" in h.twitch.sent[-1][1]


async def test_sent_reply_links_to_its_command_run(h: Harness) -> None:
    await h.say("alice", "!ping")
    await h.settle()
    runs = await h.rows("SELECT run_ref FROM command_runs")
    sent = await h.rows("SELECT run_ref FROM outbound_msgs")
    assert runs == sent and runs[0][0]


async def test_default_emoji_prefix_with_or_without_a_space(h: Harness) -> None:
    """The shipped default sign is 🏜, and `🏜 ping` is as valid as `🏜ping` (spec §2.1)."""
    await h.policy.mutate(
        lambda repo: repo.set_channel_field(CHANNEL_ID, "prefix", DEFAULT_PREFIX, Actor(None, "system"))
    )
    await h.say("alice", f"{DEFAULT_PREFIX}echo a")
    await h.say("alice", f"{DEFAULT_PREFIX} echo b")
    await h.say("alice", f"{DEFAULT_PREFIX}️ echo c")  # client sent the emoji-presentation form
    await h.say("alice", "!echo d")  # the old sign is ordinary chat now
    await h.settle()
    assert [text for _, text, _ in h.twitch.sent] == ["a", "b", "c"]


async def test_an_edited_publication_says_so_once_where_the_channel_asked(h: Harness) -> None:
    """ADR-0009: edits are live, so a channel can ask to hear about them as they land."""
    created = await h.commands.create(
        owner_user_id=USERS["alice"],
        owner_login="alice",
        name="hi",
        body="echo one",
        channel_id=CHANNEL_ID,
        prefix="!",
    )
    await h.commands.publish(channel_id=CHANNEL_ID, name="hi", command=created, published_by="1")
    await h.say("bob", "!hi")
    await h.settle()
    assert [text for _, text, _ in h.twitch.sent] == ["one"]

    edited = await h.commands.edit(created, "echo two", channel_id=CHANNEL_ID, prefix="!")
    await h.say("bob", "!hi")
    await h.settle()
    assert [text for _, text, _ in h.twitch.sent] == ["one", "two"]  # the notice is off by default

    await h.policy.mutate(
        lambda repo: repo.set_channel_field(CHANNEL_ID, "cc_edit_notice", True, Actor(None, "system"))
    )
    await h.commands.edit(edited, "echo three", channel_id=CHANNEL_ID, prefix="!")
    await h.say("bob", "!hi")
    await h.settle()
    assert [text for _, text, _ in h.twitch.sent][-2:] == [
        "three",
        "heads up: hi changed since v2 — @alice edited it (now v3)",
    ]

    await h.say("bob", "!hi")  # the channel has seen v3 now, so it is not told twice
    await h.settle()
    assert [text for _, text, _ in h.twitch.sent][-1] == "three"


async def test_custom_command_sees_the_stream_while_live(h: Harness) -> None:
    """{channel.live} and {channel.title} come from what the Helix poller last saw (ADR-0007)."""
    created = await h.commands.create(
        owner_user_id=USERS["alice"],
        owner_login="alice",
        name="status",
        body="echo live={channel.live} title={channel.title}",
        channel_id=CHANNEL_ID,
        prefix="!",
    )
    await h.commands.publish(channel_id=CHANNEL_ID, name="status", command=created, published_by="1")
    h.streams.streams[CHANNEL_ID] = {
        "title": "any% glitchless", "game": "DOOM", "viewers": 42, "started_at": "2026-01-01T00:00:00+00:00",
    }  # fmt: skip
    await h.say("bob", "!status")
    await h.settle()
    assert h.twitch.sent[-1][1] == "live=true title=any% glitchless"

    del h.streams.streams[CHANNEL_ID]
    await h.say("bob", "!status")
    await h.settle()
    assert h.twitch.sent[-1][1] == "missing value: {channel.title}"  # offline: no title to show


async def test_backfill_explains_itself_and_waits_for_the_broadcaster(h: Harness) -> None:
    """ADR-0008: opt-in, named at onboarding — and the prompt names the service before anything is sent."""
    await h.say("bob", "!backfill")
    await h.settle()
    said = h.twitch.sent[-1][1]
    assert said.startswith("backfill is off") and "https://history.example/api" in said

    await h.say("bob", "!backfill on")
    await h.settle()
    assert h.twitch.sent[-1][1] == "only the broadcaster can change backfill"
    assert not h.policy.channel_settings(CHANNEL_ID).history_backfill

    await h.say("doomtp", "!backfill on")
    await h.settle()
    assert h.twitch.sent[-1][1] == "backfill is on"
    assert h.policy.channel_settings(CHANNEL_ID).history_backfill

    await h.say("doomtp", "!backfill off")
    await h.settle()
    assert not h.policy.channel_settings(CHANNEL_ID).history_backfill
