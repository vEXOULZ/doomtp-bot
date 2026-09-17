"""!join / !part (ADR-0007 joining etiquette). Part of core_admin: not toggleable."""

from __future__ import annotations

from doomtp_bot.modules._common import actor, rank, user_arg
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
        summary="Invite the bot to your channel",
        description="Type !join in the bot's own chat to add the bot to your channel. Bot admins can name any channel.",
        params=(Param("1", "channel", description="Channel to join (bot admins only)"),),
        examples=(Example("!join", "joined #yourchannel"),),
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
                Code.FAIL, "only bot admins can add other channels; ask the broadcaster to type !join"
            )
        user = await user_arg(ctx, target)
        channel_id, login = user["id"], user["name"]
    else:
        if ctx.channel.id != twitch.bot_id:
            return Result.failure(
                Code.FAIL, f"type !join in #{twitch.bot_login}'s chat to add the bot to your channel"
            )
        channel_id, login = ctx.invoker.id, ctx.invoker.login
    if channels.is_active(channel_id):
        return Result.success(f"already in #{login}")
    failed = await channels.join(channel_id, login, actor(ctx))
    if failed:
        return Result.failure(Code.FAIL, f"joined #{login}, but Twitch refused: {', '.join(failed)}")
    return Result.success(f"joined #{login}", {"channel_id": channel_id, "login": login})


@command(
    CommandSpec(
        name="part",
        module=MODULE,
        aliases=("leave",),
        summary="Remove the bot from a channel",
        description="The broadcaster can type !part in their chat. Bot admins can name any channel.",
        params=(Param("1", "channel", description="Channel to leave (bot admins only)"),),
        examples=(Example("!part", "bye! leaving #yourchannel"),),
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


COMMANDS: tuple[Command, ...] = (join_cmd, part_cmd)
