"""`/auth/*`: the bot's one-time authorization, and a broadcaster connecting their channel.

`/auth/login` is for whoever runs the bot (architecture §3.1, LAN-only). `/auth/connect` is the link a
broadcaster follows to give the bot their channel's events — the full tier of ADR-0007. Both return to
`/auth/callback`, which tells them apart by the `state` Twitch hands back.

`/auth/admin/login` signs a person in to the web admin (ADR-0017) and returns to `/auth/admin/callback`,
which sets the session cookie and sends them back to the site. It never shows a page of its own: a
failure goes back to the site's login page as `?error=<reason>`.
"""

from __future__ import annotations

import html
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from doomtp_bot.api.routes.session import set_session_cookie
from doomtp_bot.twitch.auth import STATE_TTL_S, OAuthError, TwitchAuth
from doomtp_bot.twitch.signin import SignInError, TwitchSignIn, safe_next
from doomtp_bot.webui.auth import SESSION_COOKIE, AdminAuth

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


# ── signing in to the web admin (ADR-0017) ──────────────────────────────────
SIGNIN_COOKIE = "doomtp_signin"  # the state, so a callback is only taken from the browser that started it
SIGNIN_PATH = "/auth/admin"
LOGIN_PAGE = "/admin/login"  # the site's login page, which shows `?error=` and offers `?next=` again


def _back_to_login(reason: str, next_path: str) -> RedirectResponse:
    """The site's login page with a reason it can show: `not_configured`, `denied` (the user said no on
    Twitch), `expired` (start again), `twitch` (Twitch failed) or `no_channels`."""
    query = {"error": reason, **({"next": next_path} if next_path != safe_next(None) else {})}
    response = RedirectResponse(f"{LOGIN_PAGE}?{urlencode(query)}", status_code=302)
    response.delete_cookie(SIGNIN_COOKIE, path=SIGNIN_PATH)
    return response


@router.get("/admin/login")
async def admin_login(
    request: Request, next_path: str | None = Query(default=None, alias="next")
) -> Response:
    """Sign in with Twitch, then land on `next`: a path on this site, never a full URL."""
    signin: TwitchSignIn | None = getattr(request.app.state, "twitch_signin", None)
    if signin is None:
        return _back_to_login("not_configured", safe_next(next_path))
    url, state = signin.start(next_path)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        SIGNIN_COOKIE,
        state,
        max_age=STATE_TTL_S,
        path=SIGNIN_PATH,
        httponly=True,
        samesite="lax",  # Twitch's redirect back is a top-level navigation, which Lax still sends it on
        secure=request.url.scheme == "https",
    )
    return response


@router.get("/admin/callback")
async def admin_callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None
) -> Response:
    signin: TwitchSignIn | None = getattr(request.app.state, "twitch_signin", None)
    if signin is None:
        return _back_to_login("not_configured", safe_next(None))
    next_path = signin.next_for(state)
    try:
        grant, access, next_path = await signin.complete(
            code, state, error, request.cookies.get(SIGNIN_COOKIE)
        )
    except SignInError as exc:
        return _back_to_login(exc.reason, next_path)
    auth: AdminAuth = request.app.state.admin_auth
    auth.logout(request.cookies.get(SESSION_COOKIE))
    session = auth.login(
        role=access.role,
        user_id=grant.user_id,
        user_login=grant.login,
        channels=access.channels,
        grant=grant,
        checked_at=signin.clock(),
    )
    response = RedirectResponse(next_path, status_code=302)
    set_session_cookie(request, response, session)
    response.delete_cookie(SIGNIN_COOKIE, path=SIGNIN_PATH)
    return response
