"""The bot's web API with made-up data and no Twitch, for building the web site against (ADR-0016).

    docker compose --profile test up -d postgres-test
    python scripts/dev_api.py            # http://127.0.0.1:8080, admin password "admin"

It serves the same FastAPI app the bot does: the same routes, the same JSON, the same session login.
What it leaves out is everything that talks to Twitch: no chat, no EventSub, and channel joins reach a
stand-in that knows a few made-up users. The data lives in a database of its own (`doomtp_dev`, on the
test server unless `--database-url` says otherwise), dropped and seeded again at every start, so
whatever the site does to it is gone on the next run. Never point it at a real database.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import psycopg
import uvicorn

from doomtp_bot.api.app import create_app
from doomtp_bot.api.keys import ApiKeyService
from doomtp_bot.core.channels import ChannelManager
from doomtp_bot.core.health import ComponentHealth, HealthRegistry, Status
from doomtp_bot.customcmds.packs import PackService
from doomtp_bot.customcmds.resolution import CustomCommandLoader
from doomtp_bot.customcmds.service import CustomCommandService
from doomtp_bot.filters.service import FilterService
from doomtp_bot.lang.parser import Context
from doomtp_bot.modules import builtin_registry
from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.runtime.engine import Runtime
from doomtp_bot.runtime.explain import ReportStore, explain
from doomtp_bot.storage.db import Databases, configure_event_loop
from doomtp_bot.triggers.service import TriggerService
from doomtp_bot.variables.store import PostgresVariableStore

SERVER = "postgresql://postgres:postgres@127.0.0.1:55432"
SETUP = Actor(None, "system")
# Made-up Twitch users: the channels below, and a few people to ignore, join or explain as.
USERS = {"vexoulz": "1001", "doomtp": "1002", "friendlychannel": "1003", "alice": "2001", "pest": "2002"}


class StandInTwitch:
    """Answers the few questions the API asks Twitch, from `USERS`. Subscriptions always succeed."""

    bot_id = "999"

    async def resolve_user(self, login: str) -> dict[str, str] | None:
        name = login.lower().lstrip("@")
        return {"id": USERS[name], "name": name, "display": name.title()} if name in USERS else None

    async def subscribe_channel(self, channel_id: str) -> list[str]:
        return []

    async def unsubscribe_channel(self, channel_id: str) -> None: ...


class NoSessions:
    async def start_session(self, channel_id: str) -> None: ...

    async def end_session(self, channel_id: str, reason: str) -> None: ...


async def fresh_database(server: str, name: str) -> str:
    conn = await psycopg.AsyncConnection.connect(f"{server}/postgres", autocommit=True)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
    return f"{server}/{name}"


async def seed(
    policy: PolicyService, customcmds: CustomCommandService, packs: PackService, **svc: Any
) -> None:
    """Two joined channels and one the bot was banned from, published commands and packs, a trigger,
    a filter and an ignored user."""

    async def channels(repo: Any) -> None:
        for login, status in (("vexoulz", "joined"), ("doomtp", "joined"), ("friendlychannel", "banned")):
            await repo.ensure_channel(USERS[login], login, SETUP)
            await repo.set_channel_field(USERS[login], "status", status, SETUP)

    await policy.mutate(channels)
    vex = USERS["vexoulz"]
    await policy.mutate(lambda repo: repo.set_channel_field(vex, "prefix", "!", SETUP))
    await policy.mutate(
        lambda repo: repo.set_ignored(vex, USERS["pest"], "pest", True, Actor(vex, "chat"), reason="spam")
    )
    await policy.mutate(  # `ignore me` in chat: alice may take this one back herself
        lambda repo: repo.set_ignored(vex, USERS["alice"], "alice", True, Actor(USERS["alice"], "chat"))
    )

    alice = USERS["alice"]
    made = {}
    for name, body, summary, scope in (
        ("dice", "random 1-6 | echo {chatter.display} rolled {1}", "Roll a six-sided die", GLOBAL),
        ("coin", "random 1-2 | echo coin says {1}", "Flip a coin", GLOBAL),
        ("hype", "echo HYPE HYPE HYPE", "Get chat going", vex),
        ("lurk", "echo thanks for the lurk, {chatter.display}", "Say you're lurking", vex),
    ):
        command = await customcmds.create(
            owner_user_id=alice, owner_login="alice", name=name, body=body, channel_id=scope, prefix="!"
        )  # fmt: skip
        await customcmds.set_summary(command, summary)
        made[name] = command
    await customcmds.publish(channel_id=vex, name="hype", command=made["hype"], published_by=vex)
    games = await packs.create(owner_user_id=alice, name="games", summary="Little games for chat")
    for name in ("dice", "coin"):
        await packs.add_member(games, made[name])
    await packs.publish(channel_id=GLOBAL, pack=games, published_by="999")
    chill = await packs.create(owner_user_id=alice, name="chill", summary="Quiet-stream helpers")
    await packs.add_member(chill, made["lurk"])
    await packs.publish(channel_id=vex, pack=chill, published_by=vex)

    await svc["triggers"].add(
        channel_id=vex, type_="listener", expr="echo hi {chatter.display}", match={"regex": "^hello"}, run_as_rank=80,
        created_by=vex, prefix="!", via="chat",
    )  # fmt: skip
    await svc["filters"].add(channel_id=vex, pattern="badword", actor_user_id=vex, via="chat")


async def main(args: argparse.Namespace) -> None:
    dsn = await fresh_database(args.server, args.database)
    dbs = await Databases.open(dsn)
    policy = PolicyService(dbs.bot)
    await policy.reload()
    filters = FilterService(dbs.bot)
    await filters.reload()
    customcmds = CustomCommandService(dbs.bot, filters=filters)
    packs = PackService(dbs.bot, customcmds)
    triggers = TriggerService(dbs.bot, filters=filters)
    await triggers.reload()
    twitch = StandInTwitch()
    runtime = Runtime(
        builtin_registry(),
        policy=policy,
        custom=CustomCommandLoader(customcmds, packs),  # without it, explain knows no custom command
        services={"policy": policy, "packs": packs, "customcmds": customcmds},
    )
    triggers.parser_params = runtime.parser_params
    await seed(policy, customcmds, packs, triggers=triggers, filters=filters)
    await triggers.reload()

    reports = ReportStore(f"http://{args.host}:{args.port}", ttl_s=24 * 3600)
    info = policy.channel_info(USERS["vexoulz"], "vexoulz")
    report = await explain(
        runtime, "!role list", runtime.make_context(channel=info, invoker=None), context=Context.LINE
    )
    token = reports.keep({**report.as_dict(), "channel": "vexoulz"})

    health = HealthRegistry()

    async def ok() -> ComponentHealth:
        return ComponentHealth(Status.OK, {"dev": True})

    async def no_twitch() -> ComponentHealth:
        return ComponentHealth(Status.DISABLED, {"reason": "scripts/dev_api.py has no Twitch"})

    health.register("databases", ok)
    health.register("twitch", no_twitch)
    app = create_app(
        health,
        None,
        runtime=runtime,
        policy=policy,
        services={
            "customcmds": customcmds,
            "packs": packs,
            "triggers": triggers,
            "filters": filters,
            "health": health,
            "channels": ChannelManager(policy, twitch, NoSessions()),  # type: ignore[arg-type]
            "twitch": twitch,
            "variable_store": PostgresVariableStore(dbs.bot),
            "bot_db": dbs.bot,
            "chatlog_db": dbs.chatlog,
            "api_keys": ApiKeyService(dbs.bot),
            "explain_reports": reports,
        },
        admin_password=args.password,
    )
    print(f"dev API on http://{args.host}:{args.port}, admin password {args.password!r}")
    print(f"an explain report: http://{args.host}:{args.port}/explain/{token}")
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="info"))
    try:
        await server.serve()
    finally:
        await dbs.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--server", default=SERVER, help="Postgres server URL, without a database name")
    parser.add_argument("--database", default="doomtp_dev", help="dropped and recreated at every start")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--password", default="admin", help="the admin password (dev only)")
    configure_event_loop()
    asyncio.run(main(parser.parse_args()))
