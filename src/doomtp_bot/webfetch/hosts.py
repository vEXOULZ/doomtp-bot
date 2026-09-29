"""The admins' list of hosts `http get` may fetch, their secrets and the request limits (ADR-0020).

Kept in memory for the fetcher, which asks on every request; every change goes to the database first and
is audited. A secret is write-only: it leaves this module only on a request to its own host. Listings
and audit rows say that a secret is set, and its kind and name, never its value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from doomtp_bot.audit.log import write_audit
from doomtp_bot.clock import now_ms
from doomtp_bot.storage.db import Connection, fetch_all, fetch_one, transaction
from doomtp_bot.webfetch.addresses import is_ip_literal
from doomtp_bot.webfetch.fetcher import CHANNEL_PER_MINUTE, HOST_PER_MINUTE, HostRule, HttpLimits, Secret

SECRET_KINDS = ("query", "header")
MAX_LIMIT = 10_000
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_HEADER = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_PARAM = re.compile(r"^[A-Za-z0-9_.\-\[\]]{1,64}$")
# Headers the fetcher sets itself or that would change what the request is.
_OWN_HEADERS = frozenset({"host", "user-agent", "accept", "cookie", "content-length", "transfer-encoding",
                          "connection", "authorization-proxy", "proxy-authorization"})  # fmt: skip


class HostError(ValueError):
    """A pattern, secret or limit that can't be stored; the message says why."""


def normalize_pattern(text: str) -> str:
    """`API.Example.com.` → `api.example.com`. A name with at least one dot, or `*.` and a name with at
    least two labels (so `*.com` can't allow a whole top-level domain). Never an address."""
    pattern = text.strip().lower().rstrip(".")
    wildcard = pattern.startswith("*.")
    name = pattern[2:] if wildcard else pattern
    labels = name.split(".")
    if is_ip_literal(name) or not all(_LABEL.match(label) for label in labels):
        raise HostError(f"{text} isn't a host name (like api.example.com or *.example.com)")
    if len(labels) < 2 or len(name) > 253:
        raise HostError(f"{text} needs a dot: a bare name like localhost can't be allowed")
    if labels[-1].isdigit():
        raise HostError(f"{text} isn't a host name")
    return pattern


def check_secret(kind: str, name: str, value: str) -> None:
    if kind not in SECRET_KINDS:
        raise HostError("a secret goes in the query or in a header")
    if kind == "header" and (not _HEADER.match(name) or name.lower() in _OWN_HEADERS):
        raise HostError(f"{name} can't be the secret's header")
    if kind == "query" and not _PARAM.match(name):
        raise HostError(f"{name} can't be the secret's query parameter")
    if not value or len(value) > 512 or any(ord(c) < 0x21 or ord(c) == 0x7F for c in value):
        raise HostError("the secret must be 1–512 characters, with no spaces or control characters")


@dataclass(frozen=True, slots=True)
class HostEntry:
    rule: HostRule
    secret: Secret | None
    added_at: int
    added_by: str | None

    def public(self) -> dict[str, Any]:
        """What a listing may show: the secret's kind and name, never its value."""
        secret = None if self.secret is None else {"kind": self.secret.kind, "name": self.secret.name}
        return {"pattern": self.rule.pattern, "plain_http": self.rule.plain_http, "secret": secret,
                "added_at": self.added_at, "added_by": self.added_by}  # fmt: skip


class HostStore:
    """The `HostPolicy` the live fetcher reads (`webfetch.fetcher`), backed by `http_hosts`."""

    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self._entries: dict[str, HostEntry] = {}
        self._limits = HttpLimits(CHANNEL_PER_MINUTE, HOST_PER_MINUTE)

    async def reload(self) -> None:
        rows = await fetch_all(self.conn, "SELECT * FROM http_hosts ORDER BY pattern")
        self._entries = {row["pattern"]: _entry(row) for row in rows}
        limits = await fetch_one(self.conn, "SELECT channel_per_minute, host_per_minute FROM http_limits")
        if limits is not None:
            self._limits = HttpLimits(limits["channel_per_minute"], limits["host_per_minute"])

    # ── HostPolicy ──────────────────────────────────────────────────────────
    def rules(self) -> list[HostRule]:
        return [entry.rule for entry in self._entries.values()]

    def secret_for(self, pattern: str) -> Secret | None:
        entry = self._entries.get(pattern)
        return None if entry is None else entry.secret

    def limits(self) -> HttpLimits:
        return self._limits

    # ── reads for !admin and the API ────────────────────────────────────────
    def entries(self) -> list[HostEntry]:
        return list(self._entries.values())

    def get(self, pattern: str) -> HostEntry | None:
        return self._entries.get(pattern)

    # ── writes ──────────────────────────────────────────────────────────────
    async def allow(self, text: str, *, plain_http: bool = False, actor: str | None, via: str) -> HostEntry:
        """Add a host, or change whether it may be fetched over plain http. Its secret stays."""
        pattern = normalize_pattern(text)
        before = self._entries.get(pattern)
        async with transaction(self.conn):
            await self.conn.execute(
                "INSERT INTO http_hosts (pattern, plain_http, added_at, added_by) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (pattern) DO UPDATE SET plain_http = excluded.plain_http",
                (pattern, plain_http, now_ms(), actor),
            )
            await write_audit(self.conn, action="http_hosts.allow", actor_user_id=actor, via=via, target=pattern,
                              before=None if before is None else {"plain_http": before.rule.plain_http},
                              after={"plain_http": plain_http})  # fmt: skip
        await self.reload()
        return self._entries[pattern]

    async def deny(self, text: str, *, actor: str | None, via: str) -> bool:
        """Take a host off the list, and its secret with it. False when it wasn't on the list."""
        pattern = normalize_pattern(text)
        before = self._entries.get(pattern)
        if before is None:
            return False
        async with transaction(self.conn):
            await self.conn.execute("DELETE FROM http_hosts WHERE pattern = %s", (pattern,))
            await write_audit(self.conn, action="http_hosts.deny", actor_user_id=actor, via=via, target=pattern,
                              before=_audited(before), after=None)  # fmt: skip
        await self.reload()
        return True

    async def set_secret(self, text: str, secret: Secret | None, *, actor: str | None, via: str) -> HostEntry:
        """Set or clear (`None`) a listed host's secret. The audit row names the kind and the name only."""
        pattern = normalize_pattern(text)
        before = self._entries.get(pattern)
        if before is None:
            raise HostError(f"{pattern} isn't on the list; allow it first")
        if secret is not None:
            check_secret(secret.kind, secret.name, secret.value)
        kind, name, value = (secret.kind, secret.name, secret.value) if secret else (None, None, None)
        async with transaction(self.conn):
            await self.conn.execute(
                "UPDATE http_hosts SET secret_kind = %s, secret_name = %s, secret_value = %s WHERE pattern = %s",
                (kind, name, value, pattern),
            )
            await write_audit(self.conn, action="http_hosts.secret", actor_user_id=actor, via=via, target=pattern,
                              before=_audited(before)["secret"],
                              after=None if secret is None else {"kind": kind, "name": name})  # fmt: skip
        await self.reload()
        return self._entries[pattern]

    async def set_limits(
        self,
        *,
        channel_per_minute: int | None = None,
        host_per_minute: int | None = None,
        actor: str | None,
        via: str,
    ) -> HttpLimits:
        new = HttpLimits(
            self._limits.channel_per_minute if channel_per_minute is None else channel_per_minute,
            self._limits.host_per_minute if host_per_minute is None else host_per_minute,
        )
        if not (0 <= new.channel_per_minute <= MAX_LIMIT and 0 <= new.host_per_minute <= MAX_LIMIT):
            raise HostError(f"a limit is a whole number from 0 to {MAX_LIMIT}")
        before = self._limits
        async with transaction(self.conn):
            await self.conn.execute(
                "UPDATE http_limits SET channel_per_minute = %s, host_per_minute = %s, updated_at = %s,"
                " updated_by = %s",
                (new.channel_per_minute, new.host_per_minute, now_ms(), actor),
            )
            await write_audit(self.conn, action="http_limits", actor_user_id=actor, via=via,
                              before=before.as_dict(), after=new.as_dict())  # fmt: skip
        await self.reload()
        return self._limits


def _entry(row: dict[str, Any]) -> HostEntry:
    secret = None
    if row["secret_kind"] is not None:
        secret = Secret(row["secret_kind"], row["secret_name"], row["secret_value"])
    return HostEntry(HostRule(row["pattern"], row["plain_http"]), secret, row["added_at"], row["added_by"])


def _audited(entry: HostEntry) -> dict[str, Any]:
    public = entry.public()
    return {"plain_http": public["plain_http"], "secret": public["secret"]}
