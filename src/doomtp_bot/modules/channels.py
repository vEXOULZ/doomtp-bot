"""!join / !part (ADR-0007 joining etiquette) and !backfill (ADR-0008, ADR-0024). Part of core_admin: not toggleable."""

from __future__ import annotations

from doomtp_bot.clock import now_ms
from doomtp_bot.core.channels import ChannelBanned
from doomtp_bot.history.queue import OPEN_STATES, BackfillJob, BackfillQueue, BackfillRefused
from doomtp_bot.modules._common import actor, rank, sign_of, user_arg
from doomtp_bot.policy.roles import BOT_ADMIN_RANK, BROADCASTER_RANK
from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.registry import Command, command
from doomtp_bot.runtime.result import Code, CommandError, Result
from doomtp_bot.runtime.spec import CommandSpec, Cooldown, Example, Param
from doomtp_bot.runtime.values import ConversionError, convert

MODULE = "core_admin"


@command(
    CommandSpec(
        name="join",
        module=MODULE,
        toggleable=False,
        summary="Invite the bot to your channel",
        description=(
            "Type {sign}join in the bot's own chat for the connect link. It is the same link for every"
            " channel: the broadcaster opens it and signs in to Twitch as their channel, Twitch asks what to"
            " grant, and the bot joins that channel with it (ADR-0007). Bot admins can type {sign}join basic"
            " <channel> to join any channel without a grant. The bot leaves a channel that bans it, and a bot"
            " admin brings it back only by adding rejoin; a broadcaster connecting again is already deliberate."
        ),
        params=(
            Param("1", "basic", description="basic joins without a grant (bot admins only)"),
            Param("2", "channel", description="Channel to join with basic"),
            Param(
                "3", "rejoin", choices=("rejoin",), description="Come back to a channel that banned the bot"
            ),
        ),
        examples=(
            Example(
                "{sign}join",
                "to add the bot, the broadcaster opens https://bot.example/auth/connect and signs in to Twitch as their channel",
            ),
            Example("{sign}join basic somechannel", "joined #somechannel", note="bot admins only"),
            Example(
                "{sign}join basic somechannel rejoin",
                "joined #somechannel",
                note="after it was unbanned there",
            ),
        ),
        default_cooldowns={"everyone": Cooldown(tier_s=0, user_s=30)},
    )
)
async def join_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    channels, twitch = ctx.service("channels"), ctx.service("twitch")
    if ctx.invoker is None:
        return Result.failure(Code.FAIL, "join needs a chatter")
    if args.get("basic") != "basic":  # a channel name here changes nothing: the link is the same
        return _connect_link(ctx, twitch.bot_id, twitch.bot_login)
    if rank(ctx) < BOT_ADMIN_RANK:
        return Result.failure(
            Code.FAIL,
            f"only bot admins can join with basic; type {ctx.channel.prefix}join for the connect link",
        )
    target = args.get("channel")
    if not target:
        raise CommandError(f"usage: {ctx.channel.prefix}join basic <channel> [rejoin]")
    user = await user_arg(ctx, target)
    channel_id, login = user["id"], user["name"]
    if channels.is_active(channel_id):
        return Result.success(f"already in #{login}")
    try:
        failed = await channels.join(channel_id, login, actor(ctx), rejoin=args.get("rejoin") == "rejoin")
    except ChannelBanned as exc:
        return Result.failure(
            Code.FAIL, f"{exc}. {ctx.channel.prefix}join basic {login} rejoin comes back anyway"
        )
    if failed:
        return Result.failure(Code.FAIL, f"joined #{login}, but Twitch refused: {', '.join(failed)}")
    return Result.success(
        f"joined #{login}. The chat log starts now; filling the gaps in it from elsewhere is off until"
        f" you ask for it. Type {sign_of(ctx, channel_id)}backfill in your channel to read what that means.",
        {"channel_id": channel_id, "login": login},
    )


def _connect_link(ctx: CommandContext, bot_id: str, bot_login: str) -> Result:
    """The link is the same for everyone: whoever signs in with it is the channel the bot joins."""
    if ctx.channel.id != bot_id:
        return Result.failure(
            Code.FAIL,
            f"type {sign_of(ctx, bot_id)}join in #{bot_login}'s chat to add the bot to your channel",
        )
    url = ctx.exec.services.get("connect_url")
    if not url:
        return Result.failure(Code.FAIL, "the connect page isn't set up, so the bot can't take new channels")
    return Result.success(
        f"to add the bot, the broadcaster opens {url} and signs in to Twitch as their channel",
        {"connect_url": url},
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
            " The broadcaster can also queue a backfill by hand: gaps for every hole in the log, or a"
            " duration such as 6h for that much of the recent past (ADR-0024). One job runs at a time."
        ),
        params=(
            Param(
                "1",
                "action",
                description="on, off, gaps, queue, cancel, or how far back to fetch (e.g. 6h)",
            ),
            Param("2", "job", description="The job to cancel"),
        ),
        examples=(
            Example("{sign}backfill on", "backfill is on"),
            Example("{sign}backfill 6h", "queued #12: the last 6h"),
            Example("{sign}backfill queue", "#12 running, the last 6h"),
            Example("{sign}backfill cancel 12", "cancelled #12"),
        ),
    )
)
async def backfill_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    """The consent prompt ADR-0008 asks for, and the queue ADR-0024 §5 adds."""
    policy = ctx.service("policy")
    settings = policy.channel_settings(ctx.channel.id)
    if settings is None:
        return Result.failure(Code.FAIL, "backfill is a channel setting")
    action = (args.get("action") or "").lower()
    if not action:
        provider = ctx.exec.services.get("history")
        where = getattr(provider, "base_url", "") or "a history service"
        return Result.success(
            f"backfill is {'on' if settings.history_backfill else 'off'}. When it is on, messages this"
            f" channel saw while the bot was away are fetched from {where} and added to the log, marked"
            f" as coming from there. They never run commands or triggers."
            f" {ctx.channel.prefix}backfill on|off changes it; gaps, 6h, queue and cancel queue it by hand.",
            {"enabled": settings.history_backfill, "provider": where},
        )
    queue: BackfillQueue | None = ctx.exec.services.get("backfill")
    if action == "queue":
        if queue is None:
            return Result.failure(Code.FAIL, "the backfill queue isn't running")
        jobs = [j for j in await queue.jobs(ctx.channel.id) if j.state in OPEN_STATES]
        text = "; ".join(f"#{j.id} {j.state}, {_range_text(j)}" for j in jobs) or "nothing is queued"
        return Result.success(text, {"jobs": [j.to_json() for j in jobs]})
    if rank(ctx) < BROADCASTER_RANK:
        return Result.failure(Code.DENIED, "only the broadcaster can change backfill")
    if action in ("on", "off"):
        wanted = action == "on"
        if wanted != settings.history_backfill:
            await policy.mutate(
                lambda repo: repo.set_channel_field(ctx.channel.id, "history_backfill", wanted, actor(ctx))
            )
        return Result.success(f"backfill is {'on' if wanted else 'off'}", {"enabled": wanted})
    if queue is None:
        return Result.failure(Code.FAIL, "the backfill queue isn't running")
    requested_by = f"chat:{ctx.invoker.id if ctx.invoker else '?'}"
    try:
        if action == "gaps":
            jobs = await queue.queue_gaps(ctx.channel.id, requested_by)
            ids = ", ".join(f"#{j.id}" for j in jobs)
            text = (
                f"queued {len(jobs)} gap{'s' if len(jobs) != 1 else ''}: {ids}" if jobs else "no gaps to fill"
            )
            return Result.success(text, {"jobs": [j.to_json() for j in jobs]})
        if action == "cancel":
            job_id = args.get("job") or ""
            if not job_id.isdigit():
                raise CommandError(f"usage: {ctx.channel.prefix}backfill cancel <job>")
            cancelled = await queue.cancel(ctx.channel.id, int(job_id))
            if cancelled is None:
                return Result.failure(Code.FAIL, f"#{job_id} isn't queued here")
            return Result.success(f"cancelled #{job_id}", {"job": cancelled.to_json()})
        try:
            seconds = int(await convert(action, "duration"))
        except ConversionError:
            raise CommandError("expected on, off, gaps, queue, cancel <job>, or a duration like 6h") from None
        if seconds <= 0:
            raise CommandError("the duration must be more than 0")
        now = now_ms()
        job = await queue.queue_range(ctx.channel.id, now - seconds * 1000, now, requested_by)
    except BackfillRefused as exc:
        return Result.failure(Code.FAIL, str(exc))
    if job is None:
        return Result.success("that range is already queued")
    return Result.success(f"queued #{job.id}: {_range_text(job)}", {"job": job.to_json()})


def _range_text(job: BackfillJob) -> str:
    length = (job.to_ms - job.from_ms) // 1000
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if length >= size:
            length_text = f"{length // size}{unit}"
            break
    else:
        length_text = f"{length}s"
    if job.to_ms >= job.requested_at - 1000:
        return f"the last {length_text}"
    return f"{length_text} of chat"


COMMANDS: tuple[Command, ...] = (join_cmd, part_cmd, backfill_cmd)
