"""/api/v2/twitch/users: a Twitch user's public profile, for the chat user cards (ADR-0035).

The sites' chat (doomtp-web's log, the VOD sites' replay) shows a card when a chatter's name is clicked: their
picture, id, when the account was made, partner or affiliate, staff, and their bio. Helix needs a token, so the
bot looks them up for the sites, by id when the chat line has one and by login when it doesn't.

Anyone may ask, signed in or not, since the VOD sites' viewers are signed out: at most `LOOKUPS_PER_MINUTE`
lookups that reach Twitch a minute per address. Answers are cached (an hour, ten minutes for "no such user") so a busy chat costs
Twitch one request per chatter. The VOD sites live on other origins: an `Origin` listed in
`USER_LOOKUP_ORIGINS` gets `Access-Control-Allow-Origin` back. The request is a simple GET, so there is no
preflight, and no cookie is read.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, FastAPI, Query, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response as StarletteResponse
from vex_platform.api import ApiError, ApiModel, UtcDatetime

from doomtp_bot.api.routes.session import client_address
from doomtp_bot.api.sessions import ReadLimiter

log = structlog.get_logger(__name__)

FOUND_TTL_S = 3600.0
MISSING_TTL_S = 600.0
CACHE_SIZE = 5000
LOOKUPS_PER_MINUTE = 60  # Twitch lookups (cache misses) per address; a cached answer isn't counted
PATH = "/api/v2/twitch/users"
_LOGIN = r"^@?[A-Za-z0-9_]{1,25}$"
_ID = r"^[0-9]{1,20}$"


class TwitchUser(ApiModel):  # type: ignore[misc]  # vex-platform ships no type information
    id: str
    login: str
    display_name: str
    #: `staff`, `admin` or `global_mod`; null for everyone else.
    type: str | None
    #: `partner` or `affiliate`; null for everyone else.
    broadcaster_type: str | None
    description: str | None
    profile_image_url: str | None
    created_at: UtcDatetime


def parse_origins(csv: str) -> frozenset[str]:
    """`USER_LOOKUP_ORIGINS`: comma-separated origins (`https://vods.example`), without a trailing slash."""
    return frozenset(o.strip().rstrip("/") for o in csv.split(",") if o.strip())


class LookupOrigins(BaseHTTPMiddleware):
    """`Access-Control-Allow-Origin` on every answer of `PATH`, errors included (a 404 must reach the card as
    one), for the origins in `USER_LOOKUP_ORIGINS`. Nothing else on the bot is shared with other origins."""

    def __init__(self, app: Any, origins: frozenset[str]) -> None:
        super().__init__(app)
        self.origins = origins

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> StarletteResponse:
        response = await call_next(request)
        if request.url.path == PATH:
            response.headers["Vary"] = "Origin"
            origin = request.headers.get("origin")
            if origin is not None and origin in self.origins:
                response.headers["Access-Control-Allow-Origin"] = origin
        return response


def install(app: FastAPI, origins: frozenset[str]) -> None:
    """Share `PATH` with `origins` (an empty set shares it with none: same-origin sites still work)."""
    app.add_middleware(LookupOrigins, origins=origins)


def users_router() -> APIRouter:
    router = APIRouter(tags=["users"])
    limiter = ReadLimiter(reads=LOOKUPS_PER_MINUTE)
    # ("id", "123") or ("login", "name") → (fetched at, monotonic; the profile or None for no such user)
    cache: OrderedDict[tuple[str, str], tuple[float, dict[str, Any] | None]] = OrderedDict()

    def remember(key: tuple[str, str], found: dict[str, Any] | None) -> None:
        cache[key] = (time.monotonic(), found)
        cache.move_to_end(key)
        while len(cache) > CACHE_SIZE:
            cache.popitem(last=False)

    def cached(key: tuple[str, str]) -> tuple[bool, dict[str, Any] | None]:
        hit = cache.get(key)
        if hit is None:
            return False, None
        at, found = hit
        if time.monotonic() - at >= (FOUND_TTL_S if found is not None else MISSING_TTL_S):
            del cache[key]
            return False, None
        cache.move_to_end(key)
        return True, found

    @router.get("/twitch/users", response_model=TwitchUser)
    async def twitch_user(
        request: Request,
        response: Response,
        id: Annotated[str | None, Query(pattern=_ID, description="The user's Twitch id; used when given")] = None,
        login: Annotated[str | None, Query(pattern=_LOGIN, description="Their login, when the id isn't known")] = None,
    ) -> dict[str, Any]:
        """A Twitch user's public profile, by `id` or, without one, by `login`. 404 when Twitch has no such
        user (renamed, deleted or suspended)."""
        if not id and not login:
            raise ApiError(422, "invalid", "give an id or a login")
        key = ("id", id) if id else ("login", (login or "").lower().lstrip("@"))
        known, found = cached(key)
        if not known:
            wait = limiter.hit(client_address(request))
            if wait is not None:
                raise ApiError(
                    429, "rate_limited", "too many lookups; try again later", headers={"Retry-After": str(wait)}
                )
            twitch = getattr(request.app.state, "twitch", None)
            if twitch is None:
                raise ApiError(503, "unavailable", "Twitch isn't connected")
            try:
                found = await twitch.fetch_profile(**({"user_id": id} if id else {"login": key[1]}))
            except Exception as exc:
                log.warning("api.user_lookup_failed", key=key, error=str(exc))
                raise ApiError(503, "unavailable", "Twitch didn't answer") from exc
            remember(key, found)
            if found is not None:  # the same user, asked the other way, needs no second request
                remember(("id", found["id"]), found)
                remember(("login", found["login"]), found)
        if found is None:
            raise ApiError(404, "not_found", "Twitch has no such user")
        response.headers["Cache-Control"] = "public, max-age=3600"
        return found

    return router
