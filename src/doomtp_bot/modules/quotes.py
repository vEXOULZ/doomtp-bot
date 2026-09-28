"""`quote`: a channel's numbered quote list (architecture §12).

Anyone can read quotes; adding and deleting take moderator rank, and both land in the audit log. What is
added goes through the channel's filter first, because the bot reads it out later.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from doomtp_bot.modules._common import actor, rank, reject_filtered
from doomtp_bot.policy.roles import MODERATOR_RANK
from doomtp_bot.quotes import MAX_QUOTE_CHARS, Quote, QuoteService
from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.registry import Command, command
from doomtp_bot.runtime.result import Code, CommandError, Result
from doomtp_bot.runtime.spec import CommandSpec, Cooldown, Example, LogLevel, Param

MODULE = "quotes"
USAGE = "quote [number | words… | add <text> | del <number>]"


def _service(ctx: CommandContext) -> QuoteService:
    return ctx.service("quotes")  # type: ignore[no-any-return]


def _show(ctx: CommandContext, quote: Quote, suffix: str = "") -> Result:
    day = datetime.fromtimestamp(quote.added_at / 1000, ZoneInfo(ctx.channel.timezone)).date().isoformat()
    where = f"{quote.game}, {day}" if quote.game else day
    return Result.success(
        f"#{quote.number}: {quote.text} [{where}]{suffix}",
        {"number": quote.number, "text": quote.text, "game": quote.game, "added_at": quote.added_at},
    )


def _moderator(ctx: CommandContext, what: str) -> None:
    if rank(ctx) < MODERATOR_RANK:
        raise CommandError(f"{what} quotes takes moderator rank", Code.DENIED)


@command(
    CommandSpec(
        name="quote",
        module=MODULE,
        summary="Read, add and delete the channel's quotes",
        description=(
            f"{USAGE} — with nothing, a random quote; with a number, that one; with words, the newest"
            " quote containing them all. Moderators add and delete. A deleted quote's number is never"
            " given to another."
        ),
        params=(Param("1+", "arguments", description=USAGE),),
        default_cooldowns={"everyone": Cooldown(tier_s=5, user_s=15)},
        log_level=LogLevel.INVOCATIONS,
        examples=(
            Example("{sign}quote", "#12: I meant to do that [Doom, 2026-09-23]"),
            Example("{sign}quote add I meant to do that", "added #12"),
            Example("{sign}quote meant", "#12: I meant to do that [Doom, 2026-09-23]"),
        ),
    ),
    raw_tail_subcommands=(("add", 2),),
)
async def quote_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    values = list(args.values)
    service = _service(ctx)
    head = values[0].lower() if values else ""

    if head == "add":
        _moderator(ctx, "adding")
        text = (args.raw_tail or " ".join(values[1:])).strip()
        if not text:
            raise CommandError(f"usage: {ctx.channel.prefix}quote add <text>")
        if len(text) > MAX_QUOTE_CHARS:
            raise CommandError(f"a quote is at most {MAX_QUOTE_CHARS} characters")
        reject_filtered(ctx, text)
        game = ctx.channel.game if ctx.channel.live and ctx.channel.game else None
        quote = await service.add(ctx.channel.id, text, actor(ctx), game=game)
        return Result.success(f"added #{quote.number}", {"number": quote.number})

    if head in ("del", "delete"):
        _moderator(ctx, "deleting")
        if len(values) < 2 or not values[1].lstrip("#").isdigit():
            raise CommandError(f"usage: {ctx.channel.prefix}quote del <number>")
        number = int(values[1].lstrip("#"))
        if await service.delete(ctx.channel.id, number, actor(ctx)) is None:
            return Result.failure(Code.NOT_FOUND, f"there is no quote #{number}")
        return Result.success(f"deleted #{number}", {"number": number})

    if not values:
        picked = await service.pick(ctx.channel.id, ctx.rng)
        if picked is None:
            return Result.failure(Code.NOT_FOUND, f"no quotes yet — {ctx.channel.prefix}quote add <text>")
        return _show(ctx, picked)

    if len(values) == 1 and values[0].lstrip("#").isdigit():
        number = int(values[0].lstrip("#"))
        found = await service.get(ctx.channel.id, number)
        return _show(ctx, found) if found else Result.failure(Code.NOT_FOUND, f"there is no quote #{number}")

    matches = await service.search(ctx.channel.id, " ".join(values))
    if not matches:
        return Result.failure(Code.NOT_FOUND, "no quote says that")
    return _show(ctx, matches[0], f" (1 of {len(matches)})" if len(matches) > 1 else "")


COMMANDS: tuple[Command, ...] = (quote_cmd,)
