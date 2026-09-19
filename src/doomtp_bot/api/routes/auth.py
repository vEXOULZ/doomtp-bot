"""`/auth/*`: the bot's one-time authorization, and a broadcaster connecting their channel.

`/auth/login` is for whoever runs the bot (architecture §3.1, LAN-only). `/auth/connect` is the link a
broadcaster follows to give the bot their channel's events — the full tier of ADR-0007. Both return to
`/auth/callback`, which tells them apart by the `state` Twitch hands back.
"""

from __future__ import annotations

import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from doomtp_bot.twitch.auth import OAuthError, TwitchAuth

router = APIRouter(prefix="/auth", tags=["auth"])


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
        f"<body style='font-family:system-ui;max-width:40rem;margin:4rem auto;padding:0 1rem'>"
        f"<h1>{html.escape(title)}</h1><p>{body}</p>",
        status_code=status,
    )


def _auth(request: Request) -> TwitchAuth | None:
    return getattr(request.app.state, "twitch_auth", None)


@router.get("/login")
async def login(request: Request) -> Response:
    auth = _auth(request)
    if auth is None:
        return _page(
            "Twitch is not configured", "Set TWITCH_CLIENT_ID and the client secret, then restart.", 503
        )
    return RedirectResponse(auth.login_url(), status_code=302)


@router.get("/connect")
async def connect(request: Request) -> Response:
    """The link a broadcaster follows to grant their own channel (ADR-0007 full tier)."""
    auth = _auth(request)
    if auth is None:
        return _page(
            "Twitch is not configured", "Set TWITCH_CLIENT_ID and the client secret, then restart.", 503
        )
    return RedirectResponse(auth.connect_url(), status_code=302)


@router.get("/callback")
async def callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None
) -> Response:
    auth = _auth(request)
    if auth is None:
        return _page(
            "Twitch is not configured", "Set TWITCH_CLIENT_ID and the client secret, then restart.", 503
        )
    try:
        account = await auth.complete(code, state, error)
    except OAuthError as exc:
        return _page(
            "Authorization failed", html.escape(str(exc)) + " — <a href='/auth/login'>try again</a>", 400
        )
    if account.flow == "broadcaster":
        granted = ", ".join(sorted(account.scopes)) or "nothing"
        return _page(
            "Channel connected",
            f"Thanks, <b>{html.escape(account.login)}</b>. The bot now has: {html.escape(granted)}."
            " Channel point redemptions and cheers can trigger commands from here on."
            " You can take this back at any time from Twitch's <i>Connections</i> settings.",
        )
    return _page(
        "Bot authorized",
        f"Signed in as <b>{html.escape(account.login)}</b>. The bot is connecting to Twitch; you can close this page.",
    )
