"""!join / !part (ADR-0007 joining etiquette). Part of core_admin: not toggleable."""

from __future__ import annotations

from doomtp_bot.modules._common import actor, rank, sign_of, user_arg
from doomtp_bot.policy.roles import BOT_ADMIN_RANK, BROADCASTER_RANK
from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.registry import Command, command
from doomtp_bot.runtime.result import Code, Result
from doomtp_bot.runtime.spec import CommandSpec, Cooldown, Example, Param

MODULE = "core_admin"


@command(
    CommandSpec(
        name="join",
        module=MODULE,
        toggleable=False,
        summary="Invite the bot to your channel",
        description="Type {sign}join in the bot's own chat to add the bot to your channel. Bot admins can name any channel.",
        params=(Param("1", "channel", description="Channel to join (bot admins only)"),),
        examples=(Example("{sign}join", "joined #yourchannel"),),
        default_cooldowns={"everyone": Cooldown(tier_s=0, user_s=30)},
    )
)
async def join_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    channels, twitch = ctx.service("channels"), ctx.service("twitch")
    if ctx.invoker is None:
        return Result.failure(Code.FAIL, "join needs a chatter")
    target = args.get("channel")
    if target:
        if rank(ctx) < BOT_ADMIN_RANK:
            return Result.failure(
                Code.FAIL,
                "only bot admins can add other channels; ask the broadcaster to type"
                f" {ctx.channel.prefix}join",
            )
        user = await user_arg(ctx, target)
        channel_id, login = user["id"], user["name"]
    else:
        if ctx.channel.id != twitch.bot_id:
            return Result.failure(
                Code.FAIL,
                f"type {sign_of(ctx, twitch.bot_id)}join in #{twitch.bot_login}'s chat"
                " to add the bot to your channel",
            )
        channel_id, login = ctx.invoker.id, ctx.invoker.login
    if channels.is_active(channel_id):
        return Result.success(f"already in #{login}")
    failed = await channels.join(channel_id, login, actor(ctx))
    if failed:
        return Result.failure(Code.FAIL, f"joined #{login}, but Twitch refused: {', '.join(failed)}")
    return Result.success(
        f"joined #{login}. The chat log starts now; filling the gaps in it from elsewhere is off until"
        f" you ask for it — type {sign_of(ctx, channel_id)}backfill in your channel to read what that means.",
        {"channel_id": channel_id, "login": login},
    )


@command(
    CommandSpec(
        name="part",
        module=MODULE,
        toggleable=False,
        aliases=("leave",),
        summary="Remove the bot from a channel",
        description="The broadcaster can type {sign}part in their chat. Bot admins can name any channel.",
        params=(Param("1", "channel", description="Channel to leave (bot admins only)"),),
        examples=(Example("{sign}part", "bye! leaving #yourchannel"),),
    )
)
async def part_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    channels, twitch = ctx.service("channels"), ctx.service("twitch")
    target = args.get("channel")
    if target:
        if rank(ctx) < BOT_ADMIN_RANK:
            return Result.failure(Code.FAIL, "only bot admins can remove the bot from other channels")
        user = await user_arg(ctx, target)
        channel_id, login = user["id"], user["name"]
    else:
        if rank(ctx) < BROADCASTER_RANK:
            return Result.failure(Code.FAIL, "only the broadcaster can remove the bot")
        channel_id, login = ctx.channel.id, ctx.channel.login
    if channel_id == twitch.bot_id:
        return Result.failure(Code.FAIL, "the bot can't leave its own channel")
    if not channels.is_active(channel_id):
        return Result.success(f"not in #{login}")
    await channels.part(channel_id, actor(ctx))
    return Result.success(f"bye! leaving #{login}")


@command(
    CommandSpec(
        name="backfill",
        module=MODULE,
        toggleable=False,
        summary="Fill gaps in this channel's chat log from a history service",
        description=(
            "While the bot is offline nothing reaches the log. With backfill on, what it missed is"
            " fetched from a third-party history service when it comes back (ADR-0008). Off by default,"
            " because it means naming this channel to that service; only the broadcaster can change it."
        ),
        params=(Param("1", "state", required=False, choices=("on", "off"), description="Turn it on or off"),),
        examples=(Example("{sign}backfill on", "backfill is on"),),
    )
)
async def backfill_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    """The consent prompt ADR-0008 asks for: it names the service before anything is sent to it."""
    policy = ctx.service("policy")
    settings = policy.channel_settings(ctx.channel.id)
    if settings is None:
        return Result.failure(Code.FAIL, "backfill is a channel setting")
    state = args.get("state")
    if not state:
        provider = ctx.exec.services.get("history")
        where = getattr(provider, "base_url", "") or "a history service"
        return Result.success(
            f"backfill is {'on' if settings.history_backfill else 'off'}. When it is on, messages this"
            f" channel saw while the bot was away are fetched from {where} and added to the log, marked"
            f" as coming from there. They never run commands or triggers."
            f" {ctx.channel.prefix}backfill on|off changes it.",
            {"enabled": settings.history_backfill, "provider": where},
        )
    if rank(ctx) < BROADCASTER_RANK:
        return Result.failure(Code.DENIED, "only the broadcaster can change backfill")
    wanted = state.lower() == "on"
    if wanted != settings.history_backfill:
        await policy.mutate(
            lambda repo: repo.set_channel_field(ctx.channel.id, "history_backfill", wanted, actor(ctx))
        )
    return Result.success(f"backfill is {'on' if wanted else 'off'}", {"enabled": wanted})


COMMANDS: tuple[Command, ...] = (join_cmd, part_cmd, backfill_cmd)
