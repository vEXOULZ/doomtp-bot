"""What a backfilled line lacks, looked up while it is stored (ADR-0024 §3).

An IRC line names an emote by id only, and a mention or a cheermote is just text. EventSub gives all three
whole. Backfill looks them up and stores what it found in the row's `enrichment`, and the reader
(`chatlog/events.py`) puts it back into the fragments. The line itself stays as it came.

- **Emotes**, cached for good in `chatlog.emotes`: from the channel's own EventSub rows first, then Helix
  (the channel's emotes and the global ones), then the CDN, which says only whether it is animated. An
  emote none of them knows is `gone`.
- **Mentions**, as of when the message was sent: the reply's parent, then someone who spoke earlier in the
  same fill, then `user_names` nearest that time, and only then Helix, which answers for today (`twitch`).
- **Cheermotes**, in a message with bits: the channel's prefixes and tier thresholds from Helix.

Each value keeps its `source`. A lookup that fails leaves the value out rather than guessing, and never
stops the fill.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import aiohttp
import structlog

from doomtp_bot.chatlog.events import mentioned
from doomtp_bot.clock import now_ms
from doomtp_bot.core.events import ChatMessage
from doomtp_bot.storage.db import Connection, transaction

log = structlog.get_logger(__name__)

CDN = "https://static-cdn.jtvnw.net/emoticons/v2"
_LOGIN = re.compile(r"[a-z0-9_]{1,25}")
_CHEER = re.compile(r"([A-Za-z]+)(\d+)")


class Lookups(Protocol):
    """What Helix answers (`twitch.client.TwitchService`)."""

    async def fetch_emotes(self, channel_id: str) -> list[dict[str, Any]]: ...

    async def fetch_cheermotes(self, channel_id: str) -> dict[str, list[int]]: ...

    async def resolve_user(self, login: str) -> dict[str, str] | None: ...


class EmoteCdn(Protocol):
    async def formats(self, emote_id: str) -> list[str]:
        """The emote's formats, `[]` when the CDN has no such emote. Raises when the CDN can't say."""
        ...


@dataclass
class TwitchCdn:
    """Twitch's emote CDN: an animated emote has an `animated` image, and every emote a `static` one."""

    base_url: str = CDN
    _session: aiohttp.ClientSession | None = field(default=None, repr=False)

    async def formats(self, emote_id: str) -> list[str]:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        found: list[str] = []
        for kind in ("static", "animated"):
            async with self._session.get(f"{self.base_url}/{emote_id}/{kind}/dark/1.0") as response:
                if response.status == 200:
                    found.append(kind)
                elif response.status not in (403, 404):
                    raise RuntimeError(f"emote cdn answered {response.status}")
        return found

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


def _emote(set_id: Any, owner_id: Any, formats: Any, source: str) -> dict[str, Any]:
    return {"emote_set_id": set_id, "owner_id": owner_id, "format": formats, "source": source}


class Enricher:
    """Looks up what backfilled messages lack. Emotes are remembered across fills; the rest per fill."""

    def __init__(self, conn: Connection, twitch: Lookups | None, cdn: EmoteCdn | None = None) -> None:
        self.conn = conn
        self.twitch = twitch
        self.cdn = cdn
        self.emotes: dict[str, dict[str, Any]] = {}

    def fill(self, channel_id: str) -> FillEnrichment:
        return FillEnrichment(self, channel_id)


class FillEnrichment:
    """One fill of one channel: what the fill has seen so far, and Helix's answers asked once."""

    def __init__(self, enricher: Enricher, channel_id: str) -> None:
        self.enricher = enricher
        self.conn = enricher.conn
        self.channel_id = channel_id
        self._people: dict[str, dict[str, Any]] = {}  # login or display name, lower case → who
        self._names: dict[str, dict[str, Any] | None] = {}
        self._helix_emotes: dict[str, dict[str, Any]] | None = None
        self._cheermotes: dict[str, list[int]] | None = None

    async def message(self, msg: ChatMessage) -> dict[str, Any] | None:
        """The message's `enrichment`, or None when there is nothing to add."""
        try:
            found = {
                "emotes": await self._emotes([f["emote_id"] for f in msg.fragments if "emote_id" in f]),
                "mentions": await self._mentions(msg),
                "cheermotes": await self._cheers(msg) if msg.bits > 0 else {},
            }
        except Exception:
            log.exception("history.enrich_failed", message_id=msg.message_id)
            found = {}
        who = {"user_id": msg.user_id, "user_login": msg.user_login, "user_name": msg.display_name}
        self._people[msg.user_login.lower()] = self._people[msg.display_name.lower()] = who
        return {key: value for key, value in found.items() if value} or None

    # ── emotes ──────────────────────────────────────────────────────────────
    async def _emotes(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        known = self.enricher.emotes
        wanted = {i for i in ids if i not in known}
        if wanted:
            known |= await self._cached(wanted)
            wanted -= known.keys()
        if wanted:
            found = await self._from_log(wanted)
            wanted -= found.keys()
            found |= await self._from_helix(wanted)
            wanted -= found.keys()
            found |= await self._from_cdn(wanted)
            await self._remember(found)
            known |= found
        return {i: known[i] for i in ids if i in known}

    async def _cached(self, ids: set[str]) -> dict[str, dict[str, Any]]:
        async with await self.conn.execute(
            "SELECT emote_id, set_id, owner_id, formats, source FROM emotes WHERE emote_id = ANY(%s)",
            (list(ids),),
        ) as cur:
            rows = await cur.fetchall()
        return {r["emote_id"]: _emote(r["set_id"], r["owner_id"], r["formats"], r["source"]) for r in rows}

    async def _from_log(self, ids: set[str]) -> dict[str, dict[str, Any]]:
        """The emote as EventSub described it in this channel's own log."""
        async with await self.conn.execute(
            "SELECT DISTINCT ON (f->'emote'->>'id') f->'emote' AS emote FROM messages m,"
            " jsonb_array_elements(CASE WHEN jsonb_typeof(m.raw->'message'->'fragments') = 'array'"
            " THEN m.raw->'message'->'fragments' ELSE '[]' END) f"
            " WHERE m.channel_id = %s AND m.raw_format = 'eventsub' AND f->>'type' = 'emote'"
            " AND f->'emote'->>'id' = ANY(%s)",
            (self.channel_id, list(ids)),
        ) as cur:
            rows = await cur.fetchall()
        return {
            (e := r["emote"])["id"]: _emote(e.get("emote_set_id"), e.get("owner_id"), e.get("format"), "log")
            for r in rows
        }

    async def _from_helix(self, ids: set[str]) -> dict[str, dict[str, Any]]:
        if not ids or self.enricher.twitch is None:
            return {}
        if self._helix_emotes is None:
            try:
                emotes = await self.enricher.twitch.fetch_emotes(self.channel_id)
            except Exception as exc:
                log.warning("history.enrich_helix_failed", lookup="emotes", error=repr(exc))
                emotes = []
            self._helix_emotes = {
                e["id"]: _emote(e["set_id"], e["owner_id"], e["formats"], "helix") for e in emotes
            }
        return {i: self._helix_emotes[i] for i in ids if i in self._helix_emotes}

    async def _from_cdn(self, ids: set[str]) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        if self.enricher.cdn is None:
            return found
        for emote_id in sorted(ids):
            try:
                formats = await self.enricher.cdn.formats(emote_id)
            except Exception as exc:  # the CDN couldn't say: ask again next fill
                log.warning("history.enrich_cdn_failed", emote=emote_id, error=repr(exc))
                continue
            found[emote_id] = _emote(None, None, formats or None, "cdn" if formats else "gone")
        return found

    async def _remember(self, found: dict[str, dict[str, Any]]) -> None:
        if not found:
            return
        async with transaction(self.conn):
            for emote_id, e in found.items():
                await self.conn.execute(
                    "INSERT INTO emotes (emote_id, set_id, owner_id, formats, source, looked_up_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (emote_id) DO NOTHING",
                    (emote_id, e["emote_set_id"], e["owner_id"], _json(e["format"]), e["source"], now_ms()),
                )

    # ── mentions ────────────────────────────────────────────────────────────
    async def _mentions(self, msg: ChatMessage) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        parent = {n.lower() for n in msg.reply_mentions}
        for fragment in msg.fragments:
            if fragment.get("type") != "text":
                continue
            for name in mentioned(fragment["text"]):
                if name in found:
                    continue
                if name in parent and msg.reply_parent_user_id:
                    found[name] = {"user_id": msg.reply_parent_user_id, "user_login": msg.reply_parent_login,
                                   "user_name": msg.reply_parent_display, "source": "reply"}  # fmt: skip
                elif name in self._people:
                    found[name] = self._people[name] | {"source": "log"}
                elif (someone := await self._name(name, msg.sent_at)) is not None:
                    found[name] = someone
        return found

    async def _name(self, name: str, at: int) -> dict[str, Any] | None:
        if name not in self._names:
            self._names[name] = await self._from_names(name, at) or await self._from_twitch(name)
        return self._names[name]

    async def _from_names(self, name: str, at: int) -> dict[str, Any] | None:
        """Who went by that name nearest the time: the last to take it before, else the first after."""
        async with await self.conn.execute(
            "SELECT user_id, login, display_name FROM user_names"
            " WHERE lower(login) = %s OR lower(display_name) = %s"
            " ORDER BY seen_from <= %s DESC, abs(seen_from - %s) LIMIT 1",
            (name, name, at, at),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return {"user_id": row["user_id"], "user_login": row["login"], "user_name": row["display_name"],
                "source": "names"}  # fmt: skip

    async def _from_twitch(self, name: str) -> dict[str, Any] | None:
        if self.enricher.twitch is None or not _LOGIN.fullmatch(name):
            return None
        try:
            user = await self.enricher.twitch.resolve_user(name)
        except Exception as exc:
            log.warning("history.enrich_helix_failed", lookup="user", error=repr(exc))
            return None
        if user is None:
            return None
        return {
            "user_id": user["id"],
            "user_login": user["name"],
            "user_name": user["display"],
            "source": "twitch",
        }

    # ── cheermotes ──────────────────────────────────────────────────────────
    async def _cheers(self, msg: ChatMessage) -> dict[str, dict[str, Any]]:
        if self._cheermotes is None:
            self._cheermotes = {}
            if self.enricher.twitch is not None:
                try:
                    self._cheermotes = await self.enricher.twitch.fetch_cheermotes(self.channel_id)
                except Exception as exc:
                    log.warning("history.enrich_helix_failed", lookup="cheermotes", error=repr(exc))
        found: dict[str, dict[str, Any]] = {}
        for fragment in msg.fragments:
            if fragment.get("type") != "text":
                continue
            for token in fragment["text"].split():
                match = _CHEER.fullmatch(token)
                if match is None or (tiers := self._cheermotes.get(match[1].lower())) is None:
                    continue
                bits = int(match[2])
                tier = max((t for t in tiers if t <= bits), default=tiers[0] if tiers else 1)
                found[token.lower()] = {
                    "prefix": match[1].lower(),
                    "bits": bits,
                    "tier": tier,
                    "source": "twitch",
                }
        return found


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value)
