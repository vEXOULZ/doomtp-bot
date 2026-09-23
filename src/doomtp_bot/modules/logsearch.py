"""`logsearch`: find something said in this channel's chat log (architecture §3.3, §12).

The same full-text search as `GET /api/v1/channels/{login}/messages` (`chatlog/queries.py`), in its
chat-safe mode: a message moderators deleted, one cleared by a timeout or ban, the bot's own lines and
commands to it are never shown, so the search can't bring back what a moderator removed. Moderator by
default, because searching what one person said is closer to watching them than to chatting; a channel
can open it up with `role set`.
"""

from __future__ import annotations

from doomtp_bot.chatlog.queries import MAX_QUERY_CHARS, search_messages
from doomtp_bot.modules._common import policy_of
from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.registry import Command, command
from doomtp_bot.runtime.result import Code, CommandError, Result
from doomtp_bot.runtime.spec import CommandSpec, Cooldown, Example, LogLevel, Param

MODULE = "logsearch"
USAGE = "logsearch [@user] <words…>"
MAX_COUNTED = 50  # "50+" beyond this: the count is a hint, not a statistic
MAX_SHOWN = 5  # matches in the data, for a pipe to use


def ago(ms: int, now_ms: int) -> str:
    seconds = max(0, (now_ms - ms) // 1000)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


@command(
    CommandSpec(
        name="logsearch",
        module=MODULE,
        summary="Find something said in this channel's chat",
        description=(
            f"{USAGE} — the newest message with all the words, and how many more there are. Deleted"
            " messages, those of chatters timed out or banned since, and the bot's own lines are left out."
        ),
        params=(
            Param("1+", "words", required=True, max_len=MAX_QUERY_CHARS, description="What to look for"),
        ),
        required_role="moderator",
        default_cooldowns={"everyone": Cooldown(tier_s=5, user_s=10)},
        log_level=LogLevel.INVOCATIONS,
        examples=(
            Example("{sign}logsearch speedrun", "alice, 3h ago: the speedrun starts at 8 (1 of 4)"),
            Example("{sign}logsearch @alice speedrun", "alice, 3h ago: the speedrun starts at 8"),
        ),
    )
)
async def logsearch_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    values = list(args.values)
    user = values.pop(0).lstrip("@").lower() if values and values[0].startswith("@") else None
    words = " ".join(values).strip()
    if not words:
        raise CommandError(f"usage: {ctx.channel.prefix}{USAGE}")
    settings = policy_of(ctx).channel_settings(ctx.channel.id)
    if settings is not None and not settings.log_enabled:
        return Result.failure(Code.NOT_FOUND, "this channel's chat isn't logged")

    found = await search_messages(
        ctx.service("chatlog_db"),
        ctx.channel.id,
        words,
        limit=MAX_COUNTED + 1,
        visible_only=True,
        user_login=user,
    )
    if not found:
        return Result.failure(Code.NOT_FOUND, "nothing in the log says that")
    newest = found[0]
    now = int(ctx.exec.clock() * 1000)
    count = f"{MAX_COUNTED}+" if len(found) > MAX_COUNTED else str(len(found))
    more = f" (1 of {count})" if len(found) > 1 else ""
    shown = [
        {"login": m["user_login"], "text": m["text"], "sent_at": m["sent_at"]} for m in found[:MAX_SHOWN]
    ]
    return Result.success(
        f"{newest['display_name'] or newest['user_login']}, {ago(newest['sent_at'], now)}: {newest['text']}{more}",
        {"count": len(found[:MAX_COUNTED]), "messages": shown},
    )


COMMANDS: tuple[Command, ...] = (logsearch_cmd,)
