"""The real EventSub handshake, against Twitch's mock WebSocket server (ADR-0001 item 5).

Everything else in the suite fakes the transport: `test_dispatch.py` hands the bot domain events, and
`test_eventsub_contract.py` replays recorded frames through the parser. Neither runs what ADR-0001 called
the hard part — welcome, session id, `session_reconnect` to a new URL — so this test speaks the protocol
for real, over a socket, with TwitchIO's own client and the bot's own handlers:

    twitch event websocket start-server        # each test starts its own, on a free port

It is skipped without the Twitch CLI (`TWITCH_CLI=/path/to/twitch` if it isn't on PATH), or fails under
`--require-tools`, as in CI.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from typing import Any

import aiohttp
import pytest
from twitchio.eventsub.websockets import Websocket

from doomtp_bot.core.events import ChatNotification, Event
from doomtp_bot.twitch.client import TwitchService, _BotClient
from scripts.record_eventsub import WANTED
from tests.chat.test_eventsub_contract import envelope

CLI = os.environ.get("TWITCH_CLI") or shutil.which("twitch")
BROADCASTER, BOT = "40174384", "80730642"
#: The port the CLI reaches a running mock server on. It is fixed, so only one server can take triggers
#: at a time, whatever port its WebSocket itself listens on.
RPC_PORT = 44747


@pytest.fixture(scope="module", autouse=True)
def twitch_cli(require_tool: Callable[[bool, str], None]) -> None:
    require_tool(
        CLI is not None,
        "needs the Twitch CLI (twitch event websocket start-server): install it from"
        " https://dev.twitch.tv/docs/cli/, or set TWITCH_CLI to its path if it isn't on PATH",
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _listening(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _wait_for(ready: Callable[[], bool], what: str, *, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if ready():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


@dataclass
class MockServer:
    """The CLI's mock EventSub server, and the commands that push events at whoever is connected."""

    port: int
    process: subprocess.Popen[bytes]

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        assert CLI is not None
        # utf-8 explicitly: the CLI's ticks and crosses are not in a Windows console's own encoding.
        done = subprocess.run(
            [CLI, "event", *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        # The CLI prints ✔ or ✗ for what we asked, and then checks GitHub for a newer version of itself —
        # a check that can crash the process long after the event was already on its way. Believe the tick.
        if "✗" in done.stdout or ("✔" not in done.stdout and done.returncode != 0):
            raise AssertionError(f"twitch event {' '.join(args)}: {done.stdout.strip()}{done.stderr.strip()}")
        return done

    def trigger(self, subscription_type: str, session: str | None = None) -> None:
        """Push one event, to one session or to every connected client, as Twitch would."""
        self._run(
            "trigger", subscription_type, "--transport=websocket",
            f"--to-user={BROADCASTER}", f"--from-user={BOT}",
            *([f"--session={session}"] if session else []),
        )  # fmt: skip

    def tell_clients_to_reconnect(self) -> None:
        self._run("websocket", "reconnect")


@pytest.fixture
def server() -> Iterator[MockServer]:
    assert CLI is not None
    if _listening(RPC_PORT):
        # Triggers would go to that server instead of ours, and the test would wait for events that were
        # delivered somewhere else entirely.
        pytest.skip(f"a mock EventSub server is already running (port {RPC_PORT})")
    port = _free_port()
    process = subprocess.Popen(
        [CLI, "event", "websocket", "start-server", f"--port={port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for(lambda: _listening(port) and _listening(RPC_PORT), "the mock server to start")
        yield MockServer(port, process)
    finally:
        process.terminate()
        process.wait(timeout=10)
        _wait_for(lambda: not _listening(RPC_PORT), "the mock server to let go of its port")


class _Recorder(_BotClient):
    """The bot's own client, plus a note of every session Twitch welcomes it into."""

    def __init__(self, service: TwitchService) -> None:
        super().__init__(service, client_id="mock-client", client_secret="mock-secret", bot_id=BOT)
        self.sessions: list[str] = []
        self.welcomes: asyncio.Queue[str] = asyncio.Queue()

    async def event_websocket_welcome(self, payload: Any) -> None:
        self.sessions.append(payload.id)
        self.welcomes.put_nowait(payload.id)


@dataclass
class Listener:
    """A client connected to the mock server, and the domain events that reached the bot's sink."""

    client: _Recorder
    socket: Websocket
    #: Everything the sink has seen, in order, and the same events waiting to be read one by one.
    received: list[Event]
    arrived: asyncio.Queue[Event]

    async def next_notification(self, kind: str, *, within: float = 15.0) -> ChatNotification:
        """The next notification of this kind to arrive. A trigger is another process: it takes a moment."""
        async with asyncio.timeout(within):
            while True:
                event = await self.arrived.get()
                if isinstance(event, ChatNotification) and event.type == kind:
                    return event

    def all_of(self, kind: str) -> list[ChatNotification]:
        return [e for e in self.received if isinstance(e, ChatNotification) and e.type == kind]


@pytest.fixture
async def listener(server: MockServer) -> AsyncIterator[Listener]:
    received: list[Event] = []
    arrived: asyncio.Queue[Event] = asyncio.Queue()

    async def sink(event: Event) -> None:
        received.append(event)
        arrived.put_nowait(event)

    # No token store: nothing here logs in or sends, so it is never reached.
    service = TwitchService(
        client_id="mock-client",
        client_secret="mock-secret",
        tokens=None,
        sink=sink,  # type: ignore[arg-type]
    )
    client = _Recorder(service)
    service.client = client
    connection = Websocket(client=client, http=client._http, token_for=BOT)
    try:
        await connection.connect(url=server.url, fail_once=True)
        await client.welcomes.get()  # the welcome for this connection; what's left is a reconnect
        yield Listener(client, connection, received, arrived)
    finally:
        await connection.close()
        await client.close(save_tokens=False)


async def test_the_welcome_names_a_session_and_events_reach_the_sink(
    server: MockServer, listener: Listener
) -> None:
    assert listener.socket.session_id and listener.client.sessions == [listener.socket.session_id]

    server.trigger("channel.follow")

    # Straight off the wire: frame → TwitchIO → event_follow → mapping → the sink the dispatcher reads.
    followed = await listener.next_notification("follow")
    assert followed.channel_id == BROADCASTER and followed.user_id == BOT
    assert followed.payload["system_message"] == "testFromUser followed"


async def test_a_reconnect_message_moves_the_session_without_losing_events(
    server: MockServer, listener: Listener
) -> None:
    server.trigger("channel.cheer")
    await listener.next_notification("cheer")
    first = listener.socket.session_id

    server.tell_clients_to_reconnect()
    # Twitch sends session_reconnect with a URL and expects the old socket to be dropped once the new one
    # has been welcomed. TwitchIO does that for us; what matters here is that it really happens.
    second = await asyncio.wait_for(listener.client.welcomes.get(), 15)
    assert second != first

    server.trigger("channel.follow")
    await listener.next_notification("follow")  # the new session is the one being delivered to
    assert len(listener.all_of("cheer")) == 1  # and the old one didn't replay what it already sent


@pytest.mark.parametrize("subscription_type", [recording.type for recording in WANTED])
async def test_the_committed_fixtures_still_match_what_the_simulator_sends(
    server: MockServer, subscription_type: str
) -> None:
    """Recorded payloads go stale silently; a live frame beside them says when (ADR-0002 item 4)."""
    async with aiohttp.ClientSession() as http, http.ws_connect(server.url) as raw:
        welcome = json.loads((await asyncio.wait_for(raw.receive(), 15)).data)
        server.trigger(subscription_type, session=welcome["payload"]["session"]["id"])
        live = await _next_notification(raw)

    assert _shape(live) == _shape(envelope(subscription_type))


async def _next_notification(raw: aiohttp.ClientWebSocketResponse) -> Any:
    """The next event off the socket, past the keepalives Twitch fills the quiet with."""
    async with asyncio.timeout(20):
        while True:
            message = json.loads((await raw.receive()).data)
            if message["metadata"]["message_type"] == "notification":
                return message


def _shape(message: Any) -> Any:
    """Field names and types, without the ids and timestamps that differ every run."""
    if isinstance(message, dict):
        return {key: _shape(value) for key, value in sorted(message.items())}
    if isinstance(message, list):
        return [_shape(value) for value in message]
    return type(message).__name__
