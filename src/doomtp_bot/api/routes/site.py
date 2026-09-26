"""Public reads for a web UI served elsewhere (ADR-0016): what the server-rendered public pages show.

No authentication, and nothing here that the public pages don't already print: the channels the bot is
in, the built-in roles, the grammar, published packs, and an `!explain` report behind its chat link.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from doomtp_bot import __version__
from doomtp_bot.api.routes.data import _channel, _custom_json, _state
from doomtp_bot.lang import SYNTAX_VERSION
from doomtp_bot.lang.parser import DEFAULT_PREFIX
from doomtp_bot.policy.roles import BUILTIN_RANKS, CUSTOM_RANK_MAX, CUSTOM_RANK_MIN, GLOBAL
from doomtp_bot.webui.pages import GRAMMAR, GRAMMAR_RULES

router = APIRouter(prefix="/api/v1", tags=["site"])


@router.get("/site")
async def site(request: Request) -> dict[str, Any]:
    """What every page's chrome needs, and the channels the home page lists (active and joined)."""
    policy = getattr(request.app.state, "policy", None)
    auth = getattr(request.app.state, "admin_auth", None)
    channels = [c for c in policy.channels() if c.active and c.status == "joined"] if policy else []
    return {
        "version": __version__,
        "syntax_version": SYNTAX_VERSION,
        "default_prefix": DEFAULT_PREFIX,
        "admin_enabled": bool(auth and auth.enabled),
        "channels": [
            {"login": c.login, "prefix": c.prefix, "tier": c.tier}
            for c in sorted(channels, key=lambda c: c.login)
        ],
    }


@router.get("/roles")
async def roles() -> dict[str, Any]:
    """The built-in roles by rank, and the range channels may give roles of their own."""
    return {
        "roles": [
            {"name": name, "rank": rank} for name, rank in sorted(BUILTIN_RANKS.items(), key=lambda kv: kv[1])
        ],
        "custom_rank_range": [CUSTOM_RANK_MIN, CUSTOM_RANK_MAX],
    }


@router.get("/grammar")
async def grammar() -> dict[str, Any]:
    """The railroad grammar (spec Appendix D) the language page draws, as text and as rules.
    The diagrams themselves are the committed SVGs under `/static`."""
    return {"syntax_version": SYNTAX_VERSION, "text": GRAMMAR, "rules": GRAMMAR_RULES}


@router.get("/explain/{token}")
async def explain_report(request: Request, token: str) -> dict[str, Any]:
    """The report behind a chat `explain` link: short-lived, and only what its caller saw (§4.4)."""
    reports = getattr(request.app.state, "explain_reports", None)
    report = reports.get(token) if reports is not None else None
    if report is None:
        raise HTTPException(status_code=404, detail="no such report; they are kept for an hour")
    return report  # type: ignore[no-any-return]


async def _packs_json(request: Request, channel_id: str, *, include_global: bool) -> list[dict[str, Any]]:
    packs = _state(request, "packs")
    found = []
    for publication, pack in await packs.publications_in(channel_id, include_global=include_global):
        if publication.status != "active":
            continue
        found.append(
            {
                "name": pack.name,
                "summary": pack.summary,
                "scope": "global" if publication.channel_id == GLOBAL else "channel",
                "commands": [_custom_json(m) for m in await packs.members(pack.id)],
            }
        )
    return found


@router.get("/packs")
async def global_packs(request: Request) -> dict[str, Any]:
    """Packs published everywhere (ADR-0012): their commands work in every channel."""
    return {"packs": await _packs_json(request, GLOBAL, include_global=False)}


@router.get("/channels/{login}/packs")
async def channel_packs(request: Request, login: str) -> dict[str, Any]:
    """The packs a channel's chat can use: its own publications and the global ones."""
    settings = _channel(request, login)
    return {
        "channel": settings.login,
        "packs": await _packs_json(request, settings.channel_id, include_global=True),
    }
