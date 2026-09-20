"""Record real EventSub notification envelopes from the Twitch CLI's mock server (ADR-0002 item 4).

The adapter in `doomtp_bot.twitch.mapping` reads TwitchIO model objects, and TwitchIO builds those from
the JSON Twitch sends. Hand-written fakes can't tell us when that JSON changes shape, so the fixtures in
`tests/fixtures/eventsub/` are recorded from Twitch's own simulator and replayed through the real parser
by `tests/twitch/test_eventsub_contract.py`.

    twitch event websocket start-server --port 8998   # or let this script start one
    python scripts/record_eventsub.py --out tests/fixtures/eventsub

Every identifier and timestamp is pinned on the command line, so re-recording against a newer CLI leaves
an empty diff unless the payload itself changed — which is the whole point of committing them.

The CLI cannot trigger any `channel.chat.*` topic, so chat messages, notices and deletions are not
covered here; those adapters are tested against TwitchIO objects directly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

#: Pinned so a re-recording diffs cleanly. They are the CLI's own test accounts, not real users.
BROADCASTER = "40174384"
CHATTER = "80730642"
WHEN = "2026-01-01T12:00:00.000000000Z"
#: Fields the simulator freshly generates every run, which would otherwise be the whole diff.
VOLATILE = "5fc4e4ed-5e1b-4dd2-9a0d-0de0ad9c9a19"


@dataclass(frozen=True, slots=True)
class Recording:
    """One `twitch event trigger` invocation and the file its notification lands in."""

    type: str
    flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def filename(self) -> str:
        return f"{self.type}.json"

    def command(self, twitch: str, session: str) -> list[str]:
        return [
            twitch, "event", "trigger", self.type,
            "--transport=websocket", f"--session={session}",
            f"--to-user={BROADCASTER}", f"--from-user={CHATTER}", f"--timestamp={WHEN}",
            "--subscription-id=5fc4e4ed-5e1b-4dd2-9a0d-0de0ad9c9a19",
            "--event-id=e7c4e4ed-5e1b-4dd2-9a0d-0de0ad9c9a19",
            *self.flags,
        ]  # fmt: skip


WANTED: tuple[Recording, ...] = (
    Recording("channel.follow"),
    Recording(
        "channel.channel_points_custom_reward_redemption.add",
        (
            "--cost=500",
            "--item-id=92af127c-7326-4483-a52b-b0da0be61c01",
            "--item-name=Hydrate",
            "--event-status=unfulfilled",
        ),
    ),
    Recording("channel.cheer", ("--cost=250",)),
)


async def _record(ws: aiohttp.ClientWebSocketResponse, session: str, wanted: Recording, twitch: str) -> Any:
    """Trigger one event and return the notification the server pushes back."""
    trigger = await asyncio.create_subprocess_exec(
        *wanted.command(twitch, session), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await trigger.communicate()
    said = (out + err).decode("utf-8", "replace").strip()
    if "✗" in said or ("✔" not in said and trigger.returncode != 0):
        raise RuntimeError(f"{wanted.type}: {said}")
    while True:  # keepalives arrive on the same socket
        message = json.loads((await asyncio.wait_for(ws.receive(), timeout=15)).data)
        if message["metadata"]["message_type"] == "notification":
            return message


def _settle(message: dict[str, Any]) -> dict[str, Any]:
    """Replace what the simulator regenerates per run, so a re-recording only diffs on real changes."""
    message["metadata"]["message_id"] = VOLATILE
    message["metadata"]["message_timestamp"] = WHEN
    subscription = message["payload"]["subscription"]
    subscription["created_at"] = WHEN
    subscription["transport"]["session_id"] = "recorded_session"
    if "id" in message["payload"]["event"]:  # the redemption's own id, which the adapter passes through
        message["payload"]["event"]["id"] = VOLATILE
    return message


async def _connect(http: aiohttp.ClientSession, port: int) -> aiohttp.ClientWebSocketResponse:
    """Wait for the server to answer: with --start-server it is still opening its socket."""
    async with asyncio.timeout(20):
        while True:
            try:
                return await http.ws_connect(f"ws://127.0.0.1:{port}/ws")
            except aiohttp.ClientError:
                await asyncio.sleep(0.2)


async def record_all(port: int, twitch: str, out: Path) -> list[str]:
    written: list[str] = []
    async with aiohttp.ClientSession() as http, await _connect(http, port) as ws:
        welcome = json.loads((await asyncio.wait_for(ws.receive(), timeout=15)).data)
        session = welcome["payload"]["session"]["id"]
        for wanted in WANTED:
            message = await _record(ws, session, wanted, twitch)
            (out / wanted.filename).write_text(
                json.dumps(_settle(message), indent=2) + "\n", encoding="utf-8", newline="\n"
            )
            written.append(wanted.filename)
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", type=Path, default=Path("tests/fixtures/eventsub"))
    parser.add_argument("--port", type=int, default=8998, help="the mock server's port")
    parser.add_argument("--twitch", default="twitch", help="path to the Twitch CLI")
    parser.add_argument("--start-server", action="store_true", help="start the mock server too")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    server = None
    if args.start_server:
        server = subprocess.Popen(
            [args.twitch, "event", "websocket", "start-server", f"--port={args.port}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    try:
        written = asyncio.run(record_all(args.port, args.twitch, args.out))
    except (OSError, RuntimeError, TimeoutError) as exc:
        print(f"recording failed: {exc}", file=sys.stderr)
        print("is the mock server running? twitch event websocket start-server", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.terminate()
            with suppress(subprocess.TimeoutExpired):
                server.wait(timeout=5)
    print(f"wrote {len(written)} fixture(s) to {args.out}: {', '.join(written)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
