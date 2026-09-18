"""The language API the web editor talks to (ADR-0011): /parse, /explain, /language, /commands.

The server is the only authority on validity. The browser's Lezer grammar colours text while you type,
but every error, span and report here comes from the same parser, resolver and preflight a real run uses.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from doomtp_bot.lang import SYNTAX_VERSION
from doomtp_bot.lang.ast import Node, to_canonical
from doomtp_bot.lang.errors import HINTS, ParseError, ParseErrorCode
from doomtp_bot.lang.parser import (
    MAX_EXPR_CHARS,
    MAX_NAME_CHARS,
    MAX_PLACEHOLDER_NESTING,
    OPERATOR_TOKENS,
    REGISTERED_ROOTS,
    TYPE_NAMES,
    VAR_NAMESPACES,
    Context,
    NotACommand,
    parse,
)
from doomtp_bot.runtime.explain import explain
from doomtp_bot.runtime.namespaces import CONTEXT_ROOTS, RESERVED_EVERYWHERE
from doomtp_bot.runtime.preflight import MAX_CC_DEPTH, MAX_INVOCATIONS

router = APIRouter(prefix="/api/v1", tags=["language"])


class ParseRequest(BaseModel):
    text: str = Field(max_length=MAX_EXPR_CHARS * 2)
    context: Literal["line", "body", "trigger", "listener", "callback"] = "body"
    channel: str | None = None  # supplies the prefix and that channel's custom commands


class ExplainRequest(ParseRequest):
    run: bool = False  # evaluate too, with writes discarded and nothing sent (spec §9)


def _runtime(request: Request) -> Any:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="the runtime isn't available")
    return runtime


def _channel(request: Request, login: str | None) -> Any:
    """The named channel's info, or a neutral one so /parse works without a channel."""
    policy = getattr(request.app.state, "policy", None)
    if policy is None or login is None:
        from doomtp_bot.runtime.context import ChannelInfo

        return ChannelInfo(id="*", login=login or "*")
    for settings in policy.snapshot.channels.values():
        if settings.login == login.lower():
            return policy.channel_info(settings.channel_id, settings.login)
    raise HTTPException(status_code=404, detail=f"unknown channel {login}")


def _error(exc: ParseError) -> dict[str, Any]:
    return {
        "code": exc.code.value,
        "column": exc.column,
        "offset": exc.offset,
        "hint": exc.hint,
        "message": str(exc),
    }


def _spans(node: Node) -> list[dict[str, Any]]:
    """Invocation spans, so the editor can highlight what the server actually parsed."""
    from doomtp_bot.lang.ast import invocations

    return [
        {"index": inv.index, "name": inv.name, "start": inv.span[0], "end": inv.span[1]}
        for inv in invocations(node)
        if inv.span
    ]


@router.post("/parse")
async def parse_expression(request: Request, body: ParseRequest) -> dict[str, Any]:
    """Parse text and return the AST or the first error, with its span (ADR-0011)."""
    runtime = _runtime(request)
    channel = _channel(request, body.channel)
    context = Context(body.context)
    try:
        node = parse(body.text, context, runtime.parser_params(channel.prefix))
    except NotACommand:
        return {"ok": True, "syntax_version": SYNTAX_VERSION, "not_a_command": True}
    except ParseError as exc:
        return {"ok": False, "syntax_version": SYNTAX_VERSION, "error": _error(exc)}
    return {
        "ok": True,
        "syntax_version": SYNTAX_VERSION,
        "ast": to_canonical(node),
        "invocations": _spans(node),
    }


@router.post("/explain")
async def explain_expression(request: Request, body: ExplainRequest) -> dict[str, Any]:
    """The full explain report (spec §9). `run` evaluates with writes and sends disabled."""
    runtime = _runtime(request)
    channel = _channel(request, body.channel)
    ctx = runtime.make_context(channel=channel, invoker=None)
    report = await explain(runtime, body.text, ctx, context=Context(body.context), run=body.run)
    return report.as_dict()


@router.get("/language")
async def language(request: Request) -> dict[str, Any]:
    """Everything the editor needs for autocomplete and hover docs (ADR-0011)."""
    runtime = getattr(request.app.state, "runtime", None)
    raw_tail = (
        {c.spec.name: c.spec.raw_tail_from for c in runtime.registry.all() if c.spec.raw_tail_from}
        if runtime
        else {}
    )
    if runtime:
        raw_tail.update(
            {f"{c.spec.name} {sub}": at for c in runtime.registry.all() for sub, at in c.raw_tail_subcommands}
        )
    return {
        "syntax_version": SYNTAX_VERSION,
        "operators": list(OPERATOR_TOKENS),
        "roots": sorted(REGISTERED_ROOTS),
        "roots_by_context": {str(ctx): sorted(roots) for ctx, roots in CONTEXT_ROOTS.items()},
        "types": list(TYPE_NAMES) + ["choice"],
        "variable_namespaces": list(VAR_NAMESPACES),
        "reserved_variable_names": sorted(RESERVED_EVERYWHERE),
        "raw_tail_commands": raw_tail,
        "error_codes": {code.value: HINTS[code] for code in ParseErrorCode},
        "limits": {
            "MAX_EXPR_CHARS": MAX_EXPR_CHARS,
            "MAX_NAME_CHARS": MAX_NAME_CHARS,
            "MAX_PLACEHOLDER_NESTING": MAX_PLACEHOLDER_NESTING,
            "MAX_INVOCATIONS": MAX_INVOCATIONS,
            "MAX_CC_DEPTH": MAX_CC_DEPTH,
        },
    }


@router.get("/commands")
async def commands(request: Request) -> dict[str, Any]:
    """Every built-in command with its usage, parameters and examples (architecture §4.2)."""
    runtime = _runtime(request)
    listing = []
    for entry in runtime.registry.all():
        spec = entry.spec
        listing.append(
            {
                "name": spec.name,
                "module": spec.module,
                "aliases": list(spec.aliases),
                "summary": spec.summary,
                "description": spec.description,
                "usage": spec.usage(),
                "required_role": spec.required_role,
                "input": str(spec.input),
                "params": [
                    {
                        "position": p.position,
                        "name": p.name,
                        "type": p.type,
                        "required": p.required,
                        "description": p.description,
                    }
                    for p in spec.params
                ],
                "examples": [{"invocation": e.invocation, "output": e.output} for e in spec.examples],
                "default_cooldowns": {
                    role: {"tier_s": c.tier_s, "user_s": c.user_s}
                    for role, c in spec.default_cooldowns.items()
                },
            }
        )
    return {"syntax_version": SYNTAX_VERSION, "commands": listing}
