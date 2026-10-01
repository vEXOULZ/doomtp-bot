"""/api/v2: vex-platform's conventions (its docs/conventions.md, "API"; ADR-0027).

snake_case JSON, ISO 8601 UTC times, `{items, next_cursor}` lists, RFC 9457 problem details for errors
(v1 keeps `{detail}`), and an `X-Request-ID` on every response. The callers are v1's (`access.py`), and
`request.state.actor` is the caller as vex-platform names actors. A write refused to a caller who was
identified, or one that failed, is audited (`request.denied`, `request.failed`).

    /api/v2/jobs ...      the job runtime's runs (`chat_backfill`): admins only. A run is queued through
                          the backfill routes, which check consent and the range; here it can be read,
                          followed (`/jobs/{id}/events`), paused, resumed, retried and cancelled.
    /api/v2/job-kinds
    /api/v2/audit         the shared audit log: an admin sees every row, a moderator the rows of the
                          channels they manage (as GET /api/v1/audit), and everyone their own;
                          `actor=me` or `actor=<login>` (found through Twitch), and each row's
                          `scope_name` (the channel's login) and missing `actor_login` filled in
    /api/v2/channels/{login}/log, /log/coverage
                          the chat log as one timeline, as v1's (`v2_log.py`): the channel's moderators,
                          or anyone while its log is public
    /api/v2/docs          OpenAPI for these routes, for anyone signed in
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse
from vex_platform.api import RequestIdMiddleware, install_error_handlers
from vex_platform.audit import psycopg as audit_pg
from vex_platform.audit.model import AuditEntry
from vex_platform.audit.router import AuditRefusalsMiddleware, audit_router
from vex_platform.jobs import JobRuntime
from vex_platform.jobs.router import jobs_router

from doomtp_bot.api.access import Area, Caller, authenticate, check_area, platform_actor
from doomtp_bot.api.routes.data import _login_of
from doomtp_bot.api.v2_log import log_router
from doomtp_bot.audit.log import TABLE as AUDIT_TABLE

PREFIX = "/api/v2"
_READS = frozenset({"GET", "HEAD", "OPTIONS"})


def require_v2(area: Area) -> Callable[[Request], Awaitable[Caller]]:
    """A dependency: the caller, once they may use `area` (`access.require`, with the scope taken from the
    method). It puts the caller's actor on `request.state` as soon as they are known, so a refusal after
    that is audited as theirs."""

    async def dependency(request: Request) -> Caller:
        caller = await authenticate(request, "read" if request.method in _READS else "write")
        request.state.actor = platform_actor(caller)
        request.state.caller = caller
        check_area(caller, area, None)
        return caller

    dependency.area = area  # type: ignore[attr-defined]
    return dependency


async def visible_scopes(request: Request) -> Sequence[str] | None:
    """The channels whose audit rows the caller may read: all of them (None) for an admin, the ones a
    moderator manages, none for anyone else."""
    caller: Caller = request.state.caller
    if caller.is_admin:
        return None
    if caller.role != "moderator":
        return []
    policy = request.app.state.policy
    return [c.channel_id for c in policy.channels() if caller.manages(c.login)]


async def find_actor(request: Request, login: str) -> tuple[str, str] | None:
    """`?actor=<login>` as the Twitch user it names, so their rows from before the bot stored logins match
    too. None when Twitch doesn't know them (or can't say): the login then matches `actor_login`."""
    twitch = getattr(request.app.state, "twitch", None)
    try:
        user = await twitch.resolve_user(login) if twitch is not None else None
    except Exception:  # Twitch being down must not take the list with it
        user = None
    return None if user is None else ("user", user["id"])


async def audit_labels(request: Request, rows: list[dict[str, Any]]) -> None:
    """Each row's channel login as `scope_name`, and a user's login where the row has none (the bot
    records ids), as GET /api/v1/audit's `channel_login` and `actor_login`."""
    known = {c.channel_id: c.login for c in request.app.state.policy.channels()}
    for row in rows:
        row["scope_name"] = known.get(row["scope"] or "")
        if row["actor_kind"] == "user" and not row["actor_login"]:
            row["actor_login"] = await _login_of(request, row["actor_id"], known)


def mount(app: FastAPI, jobs: JobRuntime) -> None:
    """Add /api/v2 to `app`, over the runtime's runs and, through its pool, the shared audit table."""
    install_error_handlers(app, PREFIX)

    async def write(entry: AuditEntry) -> None:
        async with jobs.pool.connection() as conn:
            await audit_pg.record(conn, entry, table=AUDIT_TABLE)

    # The last added runs first: the request id is there for the refusals' rows.
    app.add_middleware(AuditRefusalsMiddleware, write=write, prefix=PREFIX)
    app.add_middleware(RequestIdMiddleware)

    admin, personal = require_v2("admin"), require_v2("personal")
    v2 = APIRouter(prefix=PREFIX)
    v2.include_router(jobs_router(jobs, admin, enqueue_kinds=frozenset()))
    v2.include_router(
        audit_router(
            jobs.pool.connection,
            personal,
            table=AUDIT_TABLE,
            visible_scopes=visible_scopes,
            find_actor=find_actor,
            labels=audit_labels,
        )  # fmt: skip
    )
    v2.include_router(log_router())
    schema = APIRouter()  # v2 alone, for its own OpenAPI
    schema.include_router(v2)
    docs = APIRouter(prefix=PREFIX, dependencies=[Depends(personal)], include_in_schema=False)

    @docs.get("/openapi.json")
    async def openapi() -> dict[str, Any]:
        return get_openapi(title="doomtp-bot", version="2", routes=schema.routes)

    @docs.get("/docs")
    async def swagger() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url=f"{PREFIX}/openapi.json", title="doomtp-bot /api/v2")

    app.include_router(v2)
    app.include_router(docs)
