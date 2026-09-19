"""`automod` command: whether the bot acts on incoming chat the word list would block (§9.3).

Managed with the other channel settings, and never toggleable: turning the *command* off would leave
enforcement running with no way to see or change it. The setting itself is the only switch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from doomtp_bot.core.capabilities import MODERATE
from doomtp_bot.moderation.automod import ACTIONS, MAX_TIMEOUT_S
from doomtp_bot.modules._common import actor
from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.registry import Command, command
from doomtp_bot.runtime.result import CommandError, Result
from doomtp_bot.runtime.spec import CommandSpec, Example, LogLevel, Param

if TYPE_CHECKING:
    from doomtp_bot.filters.service import FilterService
    from doomtp_bot.policy.service import PolicyService

MODULE = "core_admin"
USAGE = "automod [off | delete | timeout [seconds] | test <text>]"
MIN_TIMEOUT_S = 1


def _policy(ctx: CommandContext) -> PolicyService:
    return ctx.service("policy")  # type: ignore[no-any-return]


def _seconds(raw: str) -> int:
    if not raw.isdigit() or not MIN_TIMEOUT_S <= int(raw) <= MAX_TIMEOUT_S:
        raise CommandError(f"the timeout must be a whole number of seconds, 1–{MAX_TIMEOUT_S}")
    return int(raw)


def _describe(action: str, seconds: int) -> str:
    if action == "off":
        return "automod is off: the word list only censors what the bot says"
    if action == "timeout":
        return f"automod deletes blocked messages and times the chatter out for {seconds}s"
    return "automod deletes blocked messages"


@command(
    CommandSpec(
        name="automod",
        module=MODULE,
        toggleable=False,
        summary="Delete incoming chat the word list would block",
        description=(
            f"{USAGE} — acts on messages that a `block` filter entry matches. Moderators and the"
            " broadcaster are never actioned, and the bot must be a moderator here."
        ),
        params=(Param("1+", "arguments", required=False, description=USAGE),),
        required_role="moderator",
        requires=(MODERATE,),
        log_level=LogLevel.INVOCATIONS,
        examples=(
            Example("{sign}automod delete", "automod deletes blocked messages"),
            Example("{sign}automod timeout 600", "automod deletes blocked messages and times…"),
        ),
    )
)
async def automod_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    policy = _policy(ctx)
    settings = policy.channel_settings(ctx.channel.id)
    action = settings.automod_action if settings else "off"
    seconds = settings.automod_timeout_s if settings else 0
    values = [v.lower() for v in args.values]

    if not values or values[0] == "status":
        return Result.success(_describe(action, seconds), {"action": action, "seconds": seconds})

    if values[0] == "test":
        if len(args.values) < 2:
            raise CommandError(f"usage: {USAGE}")
        filters: FilterService = ctx.service("filters")
        result = filters.check(ctx.channel.id, " ".join(args.values[1:]))
        if not result.blocked:
            return Result.success("automod would leave that alone", {"blocked": False})
        patterns = ", ".join(result.patterns())
        would = "delete it" if action != "timeout" else f"delete it and time them out for {seconds}s"
        if action == "off":
            would = "do nothing — automod is off"
        return Result.success(f"blocked by {patterns}: automod would {would}", {"blocked": True})

    wanted = "delete" if values[0] == "on" else values[0]
    if wanted not in ACTIONS:
        raise CommandError(f"usage: {USAGE}")
    if wanted == "timeout":
        seconds = _seconds(values[1]) if len(values) > 1 else seconds
        await _write(ctx, "automod_timeout_s", seconds)
    await _write(ctx, "automod_action", wanted)
    return Result.success(_describe(wanted, seconds), {"action": wanted, "seconds": seconds})


async def _write(ctx: CommandContext, field: str, value: object) -> None:
    policy = _policy(ctx)
    if policy.channel_settings(ctx.channel.id) is None:
        raise CommandError("this channel isn't set up yet")
    await policy.mutate(lambda repo: repo.set_channel_field(ctx.channel.id, field, value, actor(ctx)))


COMMANDS: tuple[Command, ...] = (automod_cmd,)
