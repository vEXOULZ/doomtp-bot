"""`http` module: read one value from a web API, in commands a bot admin wrote (ADR-0020)."""

from __future__ import annotations

import time
from typing import Any

from yarl import URL

from doomtp_bot.lang.ast import Lit
from doomtp_bot.lang.errors import ParseError
from doomtp_bot.lang.parser import Context, parse_var_ref
from doomtp_bot.runtime.context import Args, CommandContext
from doomtp_bot.runtime.executor import store_key
from doomtp_bot.runtime.registry import Command, command
from doomtp_bot.runtime.result import CommandError, ErrorCode, Result
from doomtp_bot.runtime.spec import CommandSpec, Example, Param
from doomtp_bot.runtime.values import render
from doomtp_bot.webfetch.fetcher import Fetched, HttpError, HttpFetcher

MODULE = "http"


def _refuse(error: str, message: str, **data: Any) -> CommandError:
    return CommandError(message, ErrorCode[error], {"error": error, **data})


def parse_path(text: str) -> tuple[str | int, ...]:
    """`[current][temp_c]`, `[list][0][name]`, `["a key"]`: ADR-0018's brackets, with plain keys only."""
    if not text:
        return ()
    try:
        ref = parse_var_ref("channel.response" + text)
    except ParseError:
        raise _refuse("E_HTTP_PATH", f"not a path: {text} (write it like [current][temp_c])") from None
    steps: list[str | int] = []
    for step in ref.path:
        if not isinstance(step, Lit):
            raise _refuse("E_HTTP_PATH", "a path takes plain keys and numbers")
        steps.append(store_key(step.value))
    return tuple(steps)


def pick(value: Any, steps: tuple[str | int, ...]) -> Any:
    """The value at `steps`, or E_HTTP_PATH naming the first step that isn't there."""
    for depth, step in enumerate(steps):
        where = "".join(f"[{s}]" for s in steps[: depth + 1])
        if isinstance(value, dict) and str(step) in value:
            value = value[str(step)]
        elif isinstance(value, list) and isinstance(step, int) and -len(value) <= step < len(value):
            value = value[step]
        else:
            raise _refuse("E_HTTP_PATH", f"the answer has no {where}", path=where)
    return value


def _may_fetch(ctx: CommandContext) -> bool:
    """Only the body of a custom command whose publisher is a bot admin right now (ADR-0020)."""
    publisher = ctx.exec.publisher
    if ctx.exec.context is not Context.BODY or publisher is None:
        return False
    policy = ctx.service("policy")
    return bool(policy.is_bot_admin(publisher.id))


@command(
    CommandSpec(
        name="http",
        module=MODULE,
        summary="Read one value from a web API; only commands a bot admin wrote can use it",
        description=(
            "GET only, JSON only, from hosts a bot admin allowed. The path picks one value out of the"
            " answer; without it the whole answer is the result."
        ),
        params=(
            Param("1", "action", type="choice", required=True, choices=("get",)),
            Param("2", "url", type="url", required=True, description="https address on an allowed host"),
            Param("3", "path", description="like [current][temp_c]"),
        ),
        examples=(
            Example(
                "http get https://api.example/weather?q={arg.1} [current][temp_c] | echo {_1}°C in {arg.1}",
                "12°C in Lisbon",
                "inside a command a bot admin published",
            ),
        ),
    )
)
async def http_cmd(ctx: CommandContext, args: Args, stdin: Result | None) -> Result:
    if not _may_fetch(ctx):
        raise _refuse("E_HTTP_NOT_ALLOWED", "http runs only inside commands a bot admin published")
    steps = parse_path(args.get("path") or "")
    fetcher: HttpFetcher = ctx.service("http")
    started = time.monotonic()
    fetched: Fetched | None = None
    try:
        fetched = await fetcher.get(ctx.channel.id, args["url"])
        value = pick(fetched.value, steps)
    except HttpError as exc:
        await _log(ctx, args["url"], exc.code, str(exc), started, fetched)
        raise CommandError(str(exc), exc.code, exc.data) from None
    except CommandError as exc:
        await _log(ctx, args["url"], exc.code, str(exc), started, fetched)
        raise
    await _log(ctx, args["url"], 0, None, started, fetched)
    return Result.success(render(value), value)


async def _log(
    ctx: CommandContext, url: str, code: int, message: str | None, started: float, fetched: Fetched | None
) -> None:
    """One `command_runs` row per request: host and path, status, size and time — never the query
    string, which can carry the host's secret and whatever a chatter typed (ADR-0020 §Logging)."""
    writer = ctx.exec.services.get("chatlog_writer")
    if writer is None:
        return
    try:
        parsed = URL(url)
        shown = f"GET {parsed.scheme}://{parsed.raw_host}{parsed.raw_path}"
    except ValueError:
        shown = "GET (not a web address)"
    detail = f"{fetched.status}, {fetched.size} B{' (cached)' if fetched.cached else ''}" if fetched else None
    await writer.command_run(
        channel_id=ctx.channel.id,
        user_id=ctx.invoker.id if ctx.invoker else None,
        trigger_type="http",
        trigger_id=ctx.exec.run_id,
        expr=shown,
        resolved=[],
        code=code,
        message=" — ".join(p for p in (detail, message) if p) or None,
        duration_ms=int((time.monotonic() - started) * 1000),
        cancelled_reason=None,
    )


COMMANDS: tuple[Command, ...] = (http_cmd,)
