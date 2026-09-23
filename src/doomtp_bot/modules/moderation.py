"""`timeout` and `shoutout`: commands that act on Twitch, not just on chat (architecture §4.3).

Both need the bot to be a moderator in the channel (`requires=(MODERATE,)`, ADR-0007), so where it
isn't they are unavailable with that reason instead of failing at the Twitch API. Both declare
`side_effects=True`: the runtime re-checks the moderation index right before running them and never
runs them under `!explain --run`, and each handler checks once more right before its Helix call, since
looking the target up can take a moment in which a moderator removes the message that asked.
"""

from __future__ import annotations

from typing import Any

from doomtp_bot.core.capabilities import MODERATE
from doomtp_bot.modules._common import policy_of, rank
from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.registry import Command, command
from doomtp_bot.runtime.result import CommandError, Result
from doomtp_bot.runtime.spec import CommandSpec, Cooldown, Example, LogLevel, Param

MODULE = "moderation"
DEFAULT_TIMEOUT_S = 600
MAX_TIMEOUT_S = 1_209_600  # two weeks, Twitch's limit
MAX_REASON_CHARS = 200


def _twitch(ctx: CommandContext) -> Any:
    twitch = ctx.exec.services.get("twitch")
    if twitch is None:
        raise CommandError("not connected to Twitch")
    return twitch


def _refuse_untouchable(ctx: CommandContext, user: dict[str, str], what: str) -> None:
    """No acting on the channel, the bot itself, or anyone the policy ranks at or above the caller."""
    if user["id"] == ctx.channel.id:
        raise CommandError(f"can't {what} the broadcaster")
    if user["id"] == getattr(ctx.exec.services.get("twitch"), "bot_id", None):
        raise CommandError(f"can't {what} the bot")
    target = policy_of(ctx).build_chatter(ctx.channel.id, user["id"], user["name"])
    if target.rank >= rank(ctx):
        raise CommandError(f"can't {what} {user['display'] or user['name']}: they rank at or above you here")


@command(
    CommandSpec(
        name="timeout",
        module=MODULE,
        summary="Time a chatter out, as the bot",
        description=(
            "timeout <user> [duration] [reason…] — the duration is seconds or 10m, 1h30m and so on, up to"
            f" two weeks; {DEFAULT_TIMEOUT_S // 60} minutes if left out. Twitch shows the reason to the"
            " chatter and in the mod log, after the name of whoever asked."
        ),
        params=(
            Param("1", "user", type="user", required=True, description="Who to time out"),
            Param("2", "duration", type="duration", default=DEFAULT_TIMEOUT_S, description="How long"),
            Param("3+", "reason", max_len=MAX_REASON_CHARS, description="Shown to them"),
        ),
        required_role="moderator",
        requires=(MODERATE,),
        side_effects=True,
        log_level=LogLevel.INVOCATIONS,
        examples=(
            Example("{sign}timeout @spammer", "spammer is timed out for 10m"),
            Example("{sign}timeout @spammer 1h links again", "spammer is timed out for 1h"),
        ),
    )
)
async def timeout_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    user: dict[str, str] = args["user"]
    seconds = int(args.get("duration", DEFAULT_TIMEOUT_S))
    if not 1 <= seconds <= MAX_TIMEOUT_S:
        raise CommandError("the duration must be between 1 second and 2 weeks")
    _refuse_untouchable(ctx, user, "time out")
    twitch = _twitch(ctx)
    asked_by = ctx.invoker.login if ctx.invoker else "a trigger"
    reason = f"{asked_by}: {args.get('reason', '')}".strip().rstrip(":")

    ctx.ensure_not_cancelled()  # the last look before Twitch acts (architecture §4.3)
    if not await twitch.timeout_user(ctx.channel.id, user["id"], seconds, reason):
        raise CommandError("Twitch refused the timeout — is the bot still a moderator here?")
    name = user["display"] or user["name"]
    return Result.success(
        f"{name} is timed out for {_span(seconds)}", {"user": user["name"], "seconds": seconds}
    )


@command(
    CommandSpec(
        name="shoutout",
        module=MODULE,
        summary="Point chat at another streamer",
        description=(
            "shoutout <user> — says where to find them and what they last streamed, and, while this"
            " channel is live, sends Twitch's own shoutout card too. Twitch allows that card once every"
            " 2 minutes, and to the same streamer once an hour; the chat line goes out either way."
        ),
        params=(Param("1", "user", type="user", required=True, description="Who to shout out"),),
        required_role="moderator",
        requires=(MODERATE,),
        side_effects=True,
        default_cooldowns={"everyone": Cooldown(tier_s=10, user_s=30)},
        log_level=LogLevel.INVOCATIONS,
        examples=(Example("{sign}shoutout @friend", "Go check out Friend at twitch.tv/friend — last seen…"),),
    )
)
async def shoutout_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    user: dict[str, str] = args["user"]
    if user["id"] == ctx.channel.id:
        raise CommandError("that's this channel")
    twitch = _twitch(ctx)
    game = await twitch.last_game(user["id"])
    name = user["display"] or user["name"]
    text = f"Go check out {name} at twitch.tv/{user['name']}" + (
        f" — last seen playing {game}" if game else ""
    )

    card, why_not = False, "the channel isn't live"
    if ctx.channel.live:
        ctx.ensure_not_cancelled()  # the last look before Twitch acts (architecture §4.3)
        refused = await twitch.shoutout(ctx.channel.id, user["id"])
        card, why_not = refused is None, refused or ""
    return Result.success(text, {"user": user["name"], "game": game, "card": card, "card_skipped": why_not})


def _span(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


COMMANDS: tuple[Command, ...] = (timeout_cmd, shoutout_cmd)
