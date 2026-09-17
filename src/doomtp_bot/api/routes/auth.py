"""/auth/login and /auth/callback: one-time bot authorization (architecture §3.1). LAN-only."""

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
    return _page(
        "Bot authorized",
        f"Signed in as <b>{html.escape(account.login)}</b>. The bot is connecting to Twitch; you can close this page.",
    )
