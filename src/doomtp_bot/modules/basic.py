"""`basic` module: small everyday commands."""

from __future__ import annotations

import hashlib
import random

from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.registry import Command, command
from doomtp_bot.runtime.result import Result
from doomtp_bot.runtime.spec import CommandSpec, Cooldown, Example, Param

MODULE = "basic"


@command(
    CommandSpec(
        name="ping",
        module=MODULE,
        summary="Check that the bot is alive",
        examples=(Example("{sign}ping", "pong"),),
        default_cooldowns={"everyone": Cooldown(tier_s=5, user_s=10)},
    )
)
async def ping(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    return Result.success("pong", "pong")


@command(
    CommandSpec(
        name="random",
        module=MODULE,
        aliases=("rng",),
        summary="Pick a random whole number",
        params=(
            Param("1", "range", type="range", default={"lo": 1, "hi": 100}, description="Range like 1-100"),
            Param("2+", "seed", description="Anything; the same seed always gives the same number"),
        ),
        data_schema={"value": "int"},
        examples=(
            Example("{sign}random", "42"),
            Example("{sign}random 1-6 | echo you rolled {1}", "you rolled 4"),
            Example("{sign}random 1-100 {chatter.name} {now.date}", "17"),
        ),
        default_cooldowns={"everyone": Cooldown(tier_s=2, user_s=5)},
    )
)
async def random_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    bounds = args["range"]
    rng = ctx.rng if args.get("seed") is None else seeded(args["seed"])
    value = rng.randint(bounds["lo"], bounds["hi"])
    return Result.success(str(value), value)


def seeded(seed: str) -> random.Random:
    """A generator that depends only on `seed`, the same on every run and every Python (ADR-0019)."""
    return random.Random(int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big"))


COMMANDS: tuple[Command, ...] = (ping, random_cmd)
