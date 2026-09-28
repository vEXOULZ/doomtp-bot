"""Commands that act on Twitch, not just on chat (architecture §4.3, ADR-0019 item 3).

Most go out on the bot's own token and need the bot to be a moderator in the channel
(`requires=(MODERATE,)`, ADR-0007), so where it isn't they are unavailable with that reason instead of
failing at the Twitch API. `settitle`, `setgame`, `marker` and `raid` are things only the broadcaster
may do on Twitch, so they go out on the broadcaster's token and need the capability that grant buys
(`BROADCAST` or `RAIDS`, ADR-0007 item 5).

All declare `side_effects=True`: the runtime re-checks the moderation index right before running them
and never runs them under `!explain --run`, and each handler checks once more right before its Helix
call, since looking the target up can take a moment in which a moderator removes the message that asked.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from doomtp_bot.core.capabilities import BROADCAST, MODERATE, RAIDS
from doomtp_bot.modules._common import policy_of, rank, reject_filtered
from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.registry import Command, command
from doomtp_bot.runtime.result import Code, CommandError, Result
from doomtp_bot.runtime.spec import CommandSpec, Cooldown, Example, LogLevel, Param
from doomtp_bot.runtime.values import ConversionError, convert

MODULE = "moderation"
DEFAULT_TIMEOUT_S = 600
MAX_TIMEOUT_S = 1_209_600  # two weeks, Twitch's limit
MAX_REASON_CHARS = 200
MAX_ANNOUNCEMENT_CHARS = 500  # Twitch's limit
MAX_TITLE_CHARS = 140  # Twitch's limit, and a marker description's too
ANNOUNCE_COLORS = ("blue", "green", "orange", "purple", "primary")
SLOW_DEFAULT_S, SLOW_MIN_S, SLOW_MAX_S = 30, 3, 120  # Twitch's range for slow mode
FOLLOWERS_MAX_S = 90 * 86400  # three months, Twitch's limit
PIN_MIN_S, PIN_MAX_S = 30, 1800  # Twitch's range for a timed pin
# chatmode's words → the Helix Update Chat Settings field, and what to call it in the reply.
CHAT_MODES = {
    "slow": ("slow_mode", "slow mode"),
    "followers": ("follower_mode", "followers-only mode"),
    "subsonly": ("subscriber_mode", "subscriber-only mode"),
    "emoteonly": ("emote_mode", "emote-only mode"),
    "uniquechat": ("unique_chat_mode", "unique-chat mode"),
}


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


async def _act(ctx: CommandContext, call: Callable[[Any], Awaitable[str | None]]) -> None:
    """The last look before Twitch acts (architecture §4.3), then the Helix call. A refusal fails the
    command with Twitch's reason."""
    twitch = _twitch(ctx)
    ctx.ensure_not_cancelled()
    refused = await call(twitch)
    if refused is not None:
        raise CommandError(refused, Code.FAIL)


def _reason(ctx: CommandContext, args: Args) -> str:
    """Twitch shows the reason in the mod log under the bot's name, so it starts with whoever asked."""
    asked_by = ctx.invoker.login if ctx.invoker else "a trigger"
    return f"{asked_by}: {args.get('reason', '')}".strip().rstrip(":")


def _name(user: dict[str, str]) -> str:
    return user["display"] or user["name"]


def _replied(ctx: CommandContext, what: str) -> dict[str, str]:
    reply = ctx.exec.reply_to
    if reply is None:
        raise CommandError(f"reply to the message to {what}")
    return reply


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
    reason = _reason(ctx, args)

    ctx.ensure_not_cancelled()  # the last look before Twitch acts (architecture §4.3)
    if not await twitch.timeout_user(ctx.channel.id, user["id"], seconds, reason):
        raise CommandError("Twitch refused the timeout — is the bot still a moderator here?")
    return Result.success(
        f"{_name(user)} is timed out for {_span(seconds)}", {"user": user["name"], "seconds": seconds}
    )


@command(
    CommandSpec(
        name="ban",
        module=MODULE,
        summary="Ban a chatter, as the bot",
        description=(
            "ban <user> [reason…] — Twitch shows the reason to the chatter and in the mod log, after the"
            " name of whoever asked. `unban` lifts it."
        ),
        params=(
            Param("1", "user", type="user", required=True, description="Who to ban"),
            Param("2+", "reason", max_len=MAX_REASON_CHARS, description="Shown to them"),
        ),
        required_role="moderator",
        requires=(MODERATE,),
        side_effects=True,
        log_level=LogLevel.INVOCATIONS,
        examples=(Example("{sign}ban @spammer bot account", "Spammer is banned"),),
    )
)
async def ban_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    user: dict[str, str] = args["user"]
    _refuse_untouchable(ctx, user, "ban")
    reason = _reason(ctx, args)
    await _act(ctx, lambda t: t.ban_user(ctx.channel.id, user["id"], reason))
    return Result.success(f"{_name(user)} is banned", {"user": user["name"]})


@command(
    CommandSpec(
        name="unban",
        module=MODULE,
        aliases=("untimeout",),
        summary="Lift a chatter's ban or timeout",
        description="unban <user> — lifts a ban or a timeout, whichever they have. Also `untimeout`.",
        params=(Param("1", "user", type="user", required=True, description="Who may chat again"),),
        required_role="moderator",
        requires=(MODERATE,),
        side_effects=True,
        log_level=LogLevel.INVOCATIONS,
        examples=(Example("{sign}untimeout @friend", "Friend can chat again"),),
    )
)
async def unban_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    user: dict[str, str] = args["user"]
    await _act(ctx, lambda t: t.unban_user(ctx.channel.id, user["id"]))
    return Result.success(f"{_name(user)} can chat again", {"user": user["name"]})


@command(
    CommandSpec(
        name="warn",
        module=MODULE,
        summary="Warn a chatter, as the bot",
        description=(
            "warn <user> <reason…> — Twitch shows them the reason, and they must acknowledge it before"
            " they can chat again."
        ),
        params=(
            Param("1", "user", type="user", required=True, description="Who to warn"),
            Param("2+", "reason", required=True, max_len=MAX_REASON_CHARS, description="Shown to them"),
        ),
        required_role="moderator",
        requires=(MODERATE,),
        side_effects=True,
        log_level=LogLevel.INVOCATIONS,
        examples=(Example("{sign}warn @alice no spoilers please", "Alice is warned"),),
    )
)
async def warn_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    user: dict[str, str] = args["user"]
    _refuse_untouchable(ctx, user, "warn")
    reason = str(args["reason"])
    reject_filtered(ctx, reason)
    await _act(ctx, lambda t: t.warn_user(ctx.channel.id, user["id"], reason))
    return Result.success(f"{_name(user)} is warned", {"user": user["name"]})


@command(
    CommandSpec(
        name="announce",
        module=MODULE,
        summary="Post a highlighted announcement, as the bot",
        description=(
            f"announce [colour] <text…> — the colour is one of {', '.join(ANNOUNCE_COLORS)}; the"
            " channel's own colour if left out. The announcement is the output: nothing else is said."
        ),
        params=(
            Param(
                "1+", "text", required=True, max_len=MAX_ANNOUNCEMENT_CHARS, description="What to announce"
            ),
        ),
        required_role="moderator",
        requires=(MODERATE,),
        side_effects=True,
        default_cooldowns={"everyone": Cooldown(tier_s=5)},
        log_level=LogLevel.INVOCATIONS,
        examples=(Example("{sign}announce purple giveaway at the top of the hour", "(the announcement)"),),
    )
)
async def announce_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    first, _, rest = str(args["text"]).partition(" ")
    color, text = (
        (first.lower(), rest.strip())
        if first.lower() in ANNOUNCE_COLORS and rest.strip()
        else ("primary", str(args["text"]))
    )
    reject_filtered(ctx, text)
    await _act(ctx, lambda t: t.announce(ctx.channel.id, text, color))
    return Result.success(None, {"text": text, "color": color})


@command(
    CommandSpec(
        name="chatmode",
        module=MODULE,
        summary="Turn Twitch's chat modes on or off",
        description=(
            "chatmode slow [seconds|off] · followers [how long|off] · subsonly|emoteonly|uniquechat"
            f" [on|off] — slow mode waits {SLOW_DEFAULT_S}s unless told otherwise"
            f" ({SLOW_MIN_S}–{SLOW_MAX_S}s); followers-only lets anyone who has followed for that long"
            " chat (up to 3 months, 0 if left out)."
        ),
        params=(
            Param(
                "1", "mode", type="choice", choices=tuple(CHAT_MODES), required=True, description="Which mode"
            ),
            Param("2", "setting", description="on, off, or how long"),
        ),
        required_role="moderator",
        requires=(MODERATE,),
        side_effects=True,
        log_level=LogLevel.INVOCATIONS,
        examples=(
            Example("{sign}chatmode slow 10", "slow mode is on (10s)"),
            Example("{sign}chatmode followers 10m", "followers-only mode is on (10m)"),
            Example("{sign}chatmode emoteonly off", "emote-only mode is off"),
        ),
    )
)
async def chatmode_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    mode = str(args["mode"])
    field, label = CHAT_MODES[mode]
    setting = str(args.get("setting") or "on").lower()
    on = setting != "off"
    settings: dict[str, Any] = {field: on}
    shown = ""
    if on and mode in ("slow", "followers"):
        seconds = (SLOW_DEFAULT_S if mode == "slow" else 0) if setting == "on" else await _duration(setting)
        if mode == "slow":
            if not SLOW_MIN_S <= seconds <= SLOW_MAX_S:
                raise CommandError(f"slow mode waits between {SLOW_MIN_S} and {SLOW_MAX_S} seconds")
            settings["slow_mode_wait_time"] = seconds
        else:
            if seconds > FOLLOWERS_MAX_S:
                raise CommandError("followers-only mode goes up to 3 months")
            seconds -= seconds % 60  # Twitch counts it in minutes
            settings["follower_mode_duration"] = seconds // 60
        shown = f" ({_span(seconds)})" if seconds else ""
    elif setting not in ("on", "off"):
        raise CommandError(f"usage: chatmode {mode} [on|off]")
    await _act(ctx, lambda t: t.update_chat_settings(ctx.channel.id, settings))
    return Result.success(f"{label} is {'on' if on else 'off'}{shown}", {"mode": mode, "on": on})


async def _duration(text: str) -> int:
    try:
        return int(await convert(text, "duration"))
    except ConversionError as exc:
        raise CommandError(str(exc)) from None


@command(
    CommandSpec(
        name="clear",
        module=MODULE,
        summary="Clear the whole chat, as the bot",
        description="clear — removes every message in chat, as Twitch's /clear does. Says nothing itself.",
        required_role="moderator",
        requires=(MODERATE,),
        side_effects=True,
        log_level=LogLevel.INVOCATIONS,
        examples=(Example("{sign}clear", "(chat is cleared)"),),
    )
)
async def clear_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    await _act(ctx, lambda t: t.clear_chat(ctx.channel.id))
    return Result.success(None)


@command(
    CommandSpec(
        name="shield",
        module=MODULE,
        summary="Turn Twitch's Shield Mode on or off",
        description="shield on|off — Shield Mode applies the channel's own Shield Mode settings, set on Twitch.",
        params=(
            Param("1", "state", type="choice", choices=("on", "off"), required=True, description="on or off"),
        ),
        required_role="moderator",
        requires=(MODERATE,),
        side_effects=True,
        log_level=LogLevel.INVOCATIONS,
        examples=(Example("{sign}shield on", "Shield Mode is on"),),
    )
)
async def shield_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    on = args["state"] == "on"
    await _act(ctx, lambda t: t.shield_mode(ctx.channel.id, on))
    return Result.success(f"Shield Mode is {'on' if on else 'off'}", {"on": on})


@command(
    CommandSpec(
        name="delete",
        module=MODULE,
        summary="Delete the message you reply to",
        description=(
            "delete — send it as a reply to the message to remove. The bot's own messages can go too;"
            " the broadcaster's, and those of anyone ranked at or above you, can't. Says nothing itself."
        ),
        required_role="moderator",
        requires=(MODERATE,),
        side_effects=True,
        log_level=LogLevel.INVOCATIONS,
        examples=(Example("(as a reply) {sign}delete", "(the message is gone)"),),
    )
)
async def delete_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    reply = _replied(ctx, "delete")
    if reply["id"] and reply["id"] != getattr(ctx.exec.services.get("twitch"), "bot_id", None):
        _refuse_untouchable(ctx, reply, "delete a message from")
    twitch = _twitch(ctx)
    ctx.ensure_not_cancelled()  # the last look before Twitch acts (architecture §4.3)
    if not await twitch.delete_message(ctx.channel.id, reply["message_id"]):
        raise CommandError("Twitch refused to delete that message", Code.FAIL)
    return Result.success(None, {"user": reply["name"]})


@command(
    CommandSpec(
        name="pin",
        module=MODULE,
        summary="Pin the message you reply to",
        description=(
            f"pin [how long] — send it as a reply to the message to pin. Pinned until unpinned unless a"
            f" time is given ({PIN_MIN_S}s to {PIN_MAX_S // 60}m). Says nothing itself."
        ),
        params=(Param("1", "duration", type="duration", description="How long it stays pinned"),),
        required_role="moderator",
        requires=(MODERATE,),
        side_effects=True,
        log_level=LogLevel.INVOCATIONS,
        examples=(Example("(as a reply) {sign}pin 10m", "(the message is pinned for 10 minutes)"),),
    )
)
async def pin_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    reply = _replied(ctx, "pin")
    duration = args.get("duration")
    if duration is not None and not PIN_MIN_S <= int(duration) <= PIN_MAX_S:
        raise CommandError(f"a pin lasts between {PIN_MIN_S} seconds and {PIN_MAX_S // 60} minutes")
    await _act(ctx, lambda t: t.pin_message(ctx.channel.id, reply["message_id"], duration))
    return Result.success(None, {"user": reply["name"]})


@command(
    CommandSpec(
        name="unpin",
        module=MODULE,
        summary="Unpin a message",
        description=(
            "unpin — as a reply to the pinned message, or on its own for the last message the bot pinned."
            " Says nothing itself."
        ),
        required_role="moderator",
        requires=(MODERATE,),
        side_effects=True,
        log_level=LogLevel.INVOCATIONS,
        examples=(Example("{sign}unpin", "(the pin is gone)"),),
    )
)
async def unpin_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    reply = ctx.exec.reply_to
    message_id = reply["message_id"] if reply is not None else None
    await _act(ctx, lambda t: t.unpin_message(ctx.channel.id, message_id))
    return Result.success(None)


@command(
    CommandSpec(
        name="settitle",
        module=MODULE,
        summary="Set the stream title",
        description=(
            f"settitle <title…> — up to {MAX_TITLE_CHARS} characters. Goes out on the broadcaster's own"
            " token, so it needs the channel connected at /auth/connect with that permission."
        ),
        params=(Param("1+", "title", required=True, max_len=MAX_TITLE_CHARS, description="The new title"),),
        required_role="moderator",
        requires=(BROADCAST,),
        side_effects=True,
        log_level=LogLevel.INVOCATIONS,
        examples=(Example("{sign}settitle speedrun attempts, any%", "title set: speedrun attempts, any%"),),
    )
)
async def settitle_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    title = str(args["title"])
    reject_filtered(ctx, title)
    await _act(ctx, lambda t: t.update_channel(ctx.channel.id, title=title))
    return Result.success(f"title set: {title}", {"title": title})


@command(
    CommandSpec(
        name="setgame",
        module=MODULE,
        summary="Set the stream category",
        description=(
            "setgame <category…> — Twitch's category of that name, or else its closest match. Goes out"
            " on the broadcaster's own token, like `settitle`."
        ),
        params=(Param("1+", "category", required=True, description="The category's name"),),
        required_role="moderator",
        requires=(BROADCAST,),
        side_effects=True,
        log_level=LogLevel.INVOCATIONS,
        examples=(Example("{sign}setgame just chatting", "category set: Just Chatting"),),
    )
)
async def setgame_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    wanted = str(args["category"])
    game = await _twitch(ctx).find_game(wanted)
    if game is None:
        raise CommandError(f"Twitch has no category called {wanted}", Code.FAIL)
    await _act(ctx, lambda t: t.update_channel(ctx.channel.id, game_id=game["id"]))
    return Result.success(f"category set: {game['name']}", {"category": game["name"], "id": game["id"]})


@command(
    CommandSpec(
        name="marker",
        module=MODULE,
        summary="Place a stream marker",
        description=(
            f"marker [description…] — marks this moment of the stream for the VOD, with up to"
            f" {MAX_TITLE_CHARS} characters of description. Only while live; goes out on the broadcaster's"
            " own token, like `settitle`."
        ),
        params=(Param("1+", "description", max_len=MAX_TITLE_CHARS, description="What happened"),),
        required_role="moderator",
        requires=(BROADCAST,),
        side_effects=True,
        log_level=LogLevel.INVOCATIONS,
        examples=(Example("{sign}marker boss down", "marker placed"),),
    )
)
async def marker_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    if not ctx.channel.live:
        raise CommandError(
            "the channel isn't live, and Twitch only places markers during a stream", Code.FAIL
        )
    description = args.get("description")
    await _act(ctx, lambda t: t.stream_marker(ctx.channel.id, description))
    return Result.success("marker placed", {"description": description})


@command(
    CommandSpec(
        name="raid",
        module=MODULE,
        summary="Raid another channel",
        description=(
            "raid <user> — starts a raid, which Twitch sends after its own countdown. Goes out on the"
            " broadcaster's own token, so it needs the channel connected at /auth/connect with that"
            " permission. The broadcaster's alone unless `perm` says otherwise."
        ),
        params=(Param("1", "user", type="user", required=True, description="Who to raid"),),
        required_role="broadcaster",
        requires=(RAIDS,),
        side_effects=True,
        log_level=LogLevel.INVOCATIONS,
        examples=(Example("{sign}raid @friend", "raiding Friend — Twitch sends it after the countdown"),),
    )
)
async def raid_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    user: dict[str, str] = args["user"]
    if user["id"] == ctx.channel.id:
        raise CommandError("that's this channel")
    await _act(ctx, lambda t: t.start_raid(ctx.channel.id, user["id"]))
    return Result.success(
        f"raiding {_name(user)} — Twitch sends it after the countdown", {"user": user["name"]}
    )


@command(
    CommandSpec(
        name="shoutout",
        module=MODULE,
        summary="Send Twitch's shoutout card for another streamer",
        description=(
            "shoutout <user> — sends Twitch's own shoutout card, and says nothing in chat when it works."
            " It fails with Twitch's reason when the channel is offline or Twitch refuses: Twitch allows"
            " one card every 2 minutes, and to the same streamer once an hour. For a chat line as well,"
            " use the starter pack's `so` (ADR-0019)."
        ),
        params=(Param("1", "user", type="user", required=True, description="Who to shout out"),),
        required_role="moderator",
        requires=(MODERATE,),
        side_effects=True,
        default_cooldowns={"everyone": Cooldown(tier_s=10, user_s=30)},
        log_level=LogLevel.INVOCATIONS,
        examples=(Example("{sign}shoutout @friend", "(the shoutout card, no chat line)"),),
    )
)
async def shoutout_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    user: dict[str, str] = args["user"]
    if user["id"] == ctx.channel.id:
        raise CommandError("that's this channel")
    if not ctx.channel.live:
        raise CommandError(
            "the channel isn't live, and Twitch only sends shoutouts during a stream", Code.FAIL
        )
    ctx.ensure_not_cancelled()  # the last look before Twitch acts (architecture §4.3)
    refused = await _twitch(ctx).shoutout(ctx.channel.id, user["id"])
    if refused is not None:
        raise CommandError(refused, Code.FAIL)
    return Result.success(None, {"user": user["name"]})


def _span(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


COMMANDS: tuple[Command, ...] = (
    timeout_cmd,
    ban_cmd,
    unban_cmd,
    warn_cmd,
    shoutout_cmd,
    announce_cmd,
    chatmode_cmd,
    clear_cmd,
    shield_cmd,
    delete_cmd,
    pin_cmd,
    unpin_cmd,
    settitle_cmd,
    setgame_cmd,
    marker_cmd,
    raid_cmd,
)
