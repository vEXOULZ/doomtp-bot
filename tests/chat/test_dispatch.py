"""End-to-end without Twitch: chat events → log → runtime → outbox, plus !join/!part."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from doomtp_bot.chatlog.writer import ChatLogWriter
from doomtp_bot.core.channels import ChannelManager
from doomtp_bot.core.dispatch import Dispatcher
from doomtp_bot.core.events import Badge, ChatCleared, ChatMessage, ChatNotification, MessageDeleted
from doomtp_bot.core.outbox import Outbox, SendResult
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

    async def subscribe_channel(self, channel_id: str) -> list[str]:
        if self.refuse:
            return ["channel.chat.message"]
        if channel_id not in self.subscribed:  # idempotent, like TwitchService
            self.subscribed.append(channel_id)
        return []

    async def unsubscribe_channel(self, channel_id: str) -> None:
        self.subscribed.remove(channel_id)

    async def send_chat(self, channel_id: str, text: str, reply_to: str | None) -> SendResult:
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
        async with self.dbs.chatlog.execute(sql) as cur:
            return [tuple(r) for r in await cur.fetchall()]


@pytest.fixture
async def h(dbs: Databases) -> AsyncIterator[Harness]:
    gate.clear()
    policy = PolicyService(dbs.bot, bot_owner_ids=frozenset({"1"}))
    await policy.reload()
    writer = ChatLogWriter(dbs.chatlog, flush_interval=0.01)
    writer.start()
    twitch = FakeTwitch()
    registry = builtin_registry()
    registry.add(slowreply)
    runtime = Runtime(
        registry,
        policy=policy,
        callbacks=policy,
        resolve_user=twitch.resolve_user,
        services={"policy": policy, "twitch": twitch},
    )
    channels = ChannelManager(policy, twitch, writer)
    runtime.services["channels"] = channels
    moderation = ModerationIndex()
    outbox = Outbox(twitch, writer)
    dispatcher = Dispatcher(
        runtime=runtime, policy=policy, writer=writer, outbox=outbox, moderation=moderation, channels=channels
    )
    await channels.ensure_home(BOT_ID, BOT_LOGIN)
    await channels.subscribe_all()
    await channels.join(CHANNEL_ID, CHANNEL_LOGIN, Actor(None, "system"))
    try:
        yield Harness(dbs, policy, writer, twitch, dispatcher, channels)
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
    assert h.twitch.sent[-1][1] == "joined #other" and "500" in h.twitch.subscribed
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
    assert h.twitch.sent[-1][1] == "joined #doomtp" and CHANNEL_ID in h.twitch.subscribed
    assert await h.rows(
        f"SELECT end_reason FROM log_sessions WHERE channel_id = '{CHANNEL_ID}' ORDER BY id"
    ) == [
        ("part",),
        (None,),
    ]


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
