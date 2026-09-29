"""`basic` module: small everyday commands."""

from __future__ import annotations

import hashlib
import json
import random

from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.registry import Command, command
from doomtp_bot.runtime.result import CommandError, ErrorCode, Result, Value
from doomtp_bot.runtime.spec import CommandSpec, Cooldown, Example, Param
from doomtp_bot.runtime.values import ConversionError, convert, render

MODULE = "basic"


@command(
    CommandSpec(
        name="random",
        module=MODULE,
        aliases=("rng",),
        summary="Pick a random whole number, or one item of a list or map",
        params=(
            Param(
                "1",
                "range",
                type="any",
                default="1-100",
                description="Range like 1-100, or a list or map to pick from",
            ),
            Param("2+", "seed", description="Anything; the same seed always gives the same pick"),
        ),
        data_schema={"range": "int", "list or map": "{key, value}"},
        examples=(
            Example("{sign}random", "42"),
            Example("{sign}random 1-6 | echo you rolled {_1}", "you rolled 4"),
            Example("{sign}random 1-100 {$chatter.name} {$now.date}", "17"),
            Example('{sign}random ["rock","paper","scissors"]', "paper"),
            Example("{sign}random {channel.quotes} | echo #{_1[key]}: {_1[value]}", "#3: never again"),
        ),
        default_cooldowns={"everyone": Cooldown(tier_s=2, user_s=5)},
    )
)
async def random_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    rng = ctx.rng if args.get("seed") is None else seeded(args["seed"])
    arg = args["range"]
    try:
        bounds = await convert(arg, "range")
    except ConversionError:
        pool = arg if isinstance(arg, (list, dict)) else _collection(arg)
    else:
        value = rng.randint(bounds["lo"], bounds["hi"])
        return Result.success(str(value), value)
    if not pool:
        raise CommandError("nothing to pick from: it's empty", ErrorCode.E_EMPTY, {"error": "E_EMPTY"})
    key: int | str = rng.choice(list(pool) if isinstance(pool, dict) else range(len(pool)))
    item = pool[key]  # type: ignore[index]
    return Result.success(render(item), {"key": key, "value": item})


def _collection(arg: Value) -> list[Value] | dict[str, Value]:
    """A list or map typed as JSON text."""
    try:
        pool = json.loads(arg) if isinstance(arg, str) else None
    except ValueError:
        pool = None
    if not isinstance(pool, (list, dict)):
        raise CommandError("range: expected a range like 1-100, or a list or map")
    return pool


def seeded(seed: str) -> random.Random:
    """A generator that depends only on `seed`, the same on every run and every Python (ADR-0019)."""
    return random.Random(int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big"))


COMMANDS: tuple[Command, ...] = (random_cmd,)
