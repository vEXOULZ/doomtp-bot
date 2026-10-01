"""/api/v2/channels/{login}/log: the chat log as one timeline (ADR-0025), in v2's shape (ADR-0027).

The same reads as `GET /api/v1/channels/{login}/log` and `/log/coverage`, under the same access rule: the
channel's moderators, or anyone while its log is public (ADR-0026). What changes is the shape: times are
ISO 8601 UTC instead of epoch ms (`since` and `until` too), a page is `{items, next_cursor}`, a coverage
gap is `start`/`end` instead of `from`/`to`, and errors are problem details.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import Field
from vex_platform.api import ApiError, ApiModel, Page, UtcDatetime

from doomtp_bot.api.access import Caller
from doomtp_bot.api.routes.data import LOG_READ, _is_public
from doomtp_bot.chatlog import queries, timeline
from doomtp_bot.policy.snapshot import ChannelSettings

DEFAULT_LIMIT, MAX_LIMIT = 50, 500


class _Model(ApiModel):  # type: ignore[misc]  # vex-platform ships no type information
    pass


class UserRef(_Model):
    id: str
    login: str | None = None
    display_name: str | None = None


class CommandRun(_Model):
    """The command run a message started, or the one a reply of the bot's came from."""

    ref: str | None
    trigger_type: str | None
    trigger_id: str | None
    expr: str | None
    code: Any = None
    message: str | None


class MessageEntry(_Model):
    kind: Literal["message"]
    id: str
    at: UtcDatetime
    user: UserRef | None
    text: str
    fragments: list[dict[str, Any]]
    badges: list[dict[str, Any]]
    color: str | None
    bits: int
    message_type: str
    reply_parent_id: str | None
    reply_parent_user: UserRef | None
    reward_id: str | None
    source_channel_id: str | None
    is_self: bool
    is_command: bool
    source: str
    received_at: UtcDatetime | None
    deleted_at: UtcDatetime | None
    cleared_at: UtcDatetime | None
    run: CommandRun | None


class NotificationEntry(_Model):
    kind: Literal["notification"]
    id: str
    at: UtcDatetime
    user: UserRef | None
    type: str
    payload: dict[str, Any] = Field(description="Depends on `type`; a follow's `followed_at` is ISO 8601 too")
    source: str


class ModerationEntry(_Model):
    kind: Literal["moderation"]
    id: int
    at: UtcDatetime
    type: str
    message_id: str | None
    target: UserRef | None
    moderator: UserRef | None
    duration_s: int | None
    reason: str | None
    source: str


Entry = Annotated[MessageEntry | NotificationEntry | ModerationEntry, Field(discriminator="kind")]


class LogSession(_Model):
    started_at: UtcDatetime
    ended_at: UtcDatetime | None
    end_reason: str | None


class GapBackfill(_Model):
    complete: bool
    inserted: int | None
    error: str | None
    provider: str | None


class Gap(_Model):
    start: UtcDatetime
    end: UtcDatetime
    reason: Literal["before_log", "between_sessions", "not_listening"]
    backfill: GapBackfill | None


class Coverage(_Model):
    since: UtcDatetime
    until: UtcDatetime
    sessions: list[LogSession]
    gaps: list[Gap]
    complete: bool = Field(description="Whether the log has every message of the window Twitch let it see")


def _ms(value: dt.datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return int(value.timestamp() * 1000)


def _time(ms: int | None) -> dt.datetime | None:
    return None if ms is None else dt.datetime.fromtimestamp(ms / 1000, dt.UTC)


_TIMES = ("at", "received_at", "deleted_at", "cleared_at")


def _entry(entry: dict[str, Any]) -> dict[str, Any]:
    """A v1 timeline entry with its times as datetimes."""
    out = {**entry, **{k: _time(entry[k]) for k in _TIMES if k in entry}}
    payload = entry.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("followed_at"), int):
        out["payload"] = {**payload, "followed_at": _time(payload["followed_at"])}
    return out


def _channel(request: Request, login: str) -> ChannelSettings:
    settings = request.app.state.policy.channel_by_login(login)
    if settings is None:
        raise ApiError(404, "unknown_channel", f"no channel named {login}")
    return settings  # type: ignore[no-any-return]


def _chatlog(request: Request) -> Any:
    conn = getattr(request.app.state, "chatlog_db", None)
    if conn is None:
        raise ApiError(503, "unavailable", "the chat log isn't available")
    return conn


def log_router() -> APIRouter:
    router = APIRouter(tags=["log"])

    @router.get("/channels/{login}/log", response_model=Page[Entry])
    async def channel_log(
        request: Request,
        login: str,
        since: Annotated[dt.datetime | None, Query(description="Inclusive, ISO 8601")] = None,
        until: Annotated[dt.datetime | None, Query(description="Exclusive, ISO 8601")] = None,
        order: timeline.Order = "desc",
        kind: Annotated[
            list[timeline.Kind] | None,
            Query(description="message, notification or moderation; repeat for several"),
        ] = None,
        user: Annotated[
            str | None, Query(min_length=1, max_length=40, description="A login, old ones too")
        ] = None,
        q: Annotated[str | None, Query(min_length=1, max_length=queries.MAX_QUERY_CHARS)] = None,
        hide_removed: bool = False,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        caller: Caller = LOG_READ,
    ) -> Page[Any]:
        """The channel's messages, notifications and moderation as one timeline, a page at a time: pass
        `next_cursor` back as `cursor`, with the same filters. The public get messages and notifications
        only, without removed messages."""
        settings = _channel(request, login)
        kinds = list(kind or timeline.KINDS)
        if _is_public(caller):
            kinds, hide_removed = [k for k in kinds if k != "moderation"], True
            if not kinds:
                raise ApiError(403, "forbidden", f"only a moderator can read {login}'s moderation log")
        conn = _chatlog(request)
        user_ids = None if user is None else await timeline.user_ids_for(conn, user)
        try:
            after = None if cursor is None else timeline.Cursor.decode(cursor)
            entries, following = await timeline.read(
                conn, settings.channel_id, kinds=kinds, since=_ms(since), until=_ms(until), cursor=after,
                order=order, limit=limit, user_ids=user_ids, query=q, hide_removed=hide_removed,
            )  # fmt: skip
        except timeline.CursorError as exc:
            raise ApiError(400, "bad_cursor", str(exc)) from exc
        return Page[Any](
            items=[_entry(e) for e in entries], next_cursor=None if following is None else following.encode()
        )

    @router.get("/channels/{login}/log/coverage", response_model=Coverage)
    async def channel_log_coverage(
        request: Request,
        login: str,
        since: Annotated[dt.datetime, Query(description="ISO 8601")],
        until: Annotated[dt.datetime | None, Query(description="ISO 8601; now by default")] = None,
        caller: Caller = LOG_READ,
    ) -> dict[str, Any]:
        """When the bot was listening between `since` and `until`, and which holes backfill filled."""
        since_ms, until_ms = _ms(since), _ms(until)
        assert since_ms is not None
        if until_ms is not None and until_ms <= since_ms:
            raise ApiError(422, "invalid", "until must be after since")
        settings = _channel(request, login)
        found = await timeline.coverage(_chatlog(request), settings.channel_id, since_ms, until_ms)
        return {
            "since": _time(found["since"]),
            "until": _time(found["until"]),
            "sessions": [
                {
                    "started_at": _time(s["started_at"]),
                    "ended_at": _time(s["ended_at"]),
                    "end_reason": s["end_reason"],
                }
                for s in found["sessions"]
            ],
            "gaps": [
                {
                    "start": _time(g["from"]),
                    "end": _time(g["to"]),
                    "reason": g["reason"],
                    "backfill": g["backfill"],
                }
                for g in found["gaps"]
            ],
            "complete": found["complete"],
        }

    return router
