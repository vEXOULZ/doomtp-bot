"""API keys for `/api/v1` (architecture §11).

A key is 32 random bytes, so it needs no password hashing: there is nothing to guess and no dictionary
to try. Only the SHA-256 of the key is stored, and the key itself is shown once, when it is created.

Two scopes, because that is all the API distinguishes: `read` sees configuration and logs, `write` also
changes them. Every write still goes through the same services the chat commands use, so it lands in the
audit log with `via="api"`.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass

from doomtp_bot.clock import now_ms
from doomtp_bot.storage.db import Connection, Row, fetch_value, transaction

PREFIX = "dtb_"
SCOPES = ("read", "write")
_SELECT = "SELECT id, name, owner_user_id, scopes, created_at, last_used_at, revoked_at FROM api_keys"


class ApiKeyError(ValueError):
    """A key that can't be created as asked."""


@dataclass(frozen=True, slots=True)
class ApiKey:
    id: int
    name: str
    scopes: frozenset[str]
    owner_user_id: str | None = None
    created_at: int = 0
    last_used_at: int | None = None
    revoked_at: int | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    def allows(self, scope: str) -> bool:
        return self.active and (scope in self.scopes or "write" in self.scopes and scope == "read")


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class ApiKeyService:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    async def create(
        self, *, name: str, scopes: tuple[str, ...] = ("read",), owner_user_id: str | None = None
    ) -> tuple[ApiKey, str]:
        """Store a new key and return it with its one and only plaintext copy."""
        name = name.strip()
        if not name:
            raise ApiKeyError("a key needs a name")
        unknown = sorted(set(scopes) - set(SCOPES))
        if unknown or not scopes:
            raise ApiKeyError(f"scopes must be some of: {', '.join(SCOPES)}")
        secret = PREFIX + secrets.token_urlsafe(32)
        async with transaction(self.conn):
            key_id = await fetch_value(
                self.conn,
                "INSERT INTO api_keys (name, key_hash, owner_user_id, scopes, created_at)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (name, fingerprint(secret), owner_user_id, json.dumps(sorted(scopes)), now_ms()),
            )
        key = ApiKey(int(key_id or 0), name, frozenset(scopes), owner_user_id, now_ms())
        return key, secret

    async def verify(self, presented: str) -> ApiKey | None:
        """The key behind a `Authorization: Bearer …` header, or None. Records that it was used."""
        if not presented.startswith(PREFIX):
            return None
        async with await self.conn.execute(
            f"{_SELECT} WHERE key_hash = %s AND revoked_at IS NULL", (fingerprint(presented),)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        await self.conn.execute("UPDATE api_keys SET last_used_at = %s WHERE id = %s", (now_ms(), row["id"]))
        return _key(row)

    async def list(self, *, include_revoked: bool = False) -> list[ApiKey]:
        where = "" if include_revoked else " WHERE revoked_at IS NULL"
        async with await self.conn.execute(f"{_SELECT}{where} ORDER BY id") as cur:
            return [_key(row) for row in await cur.fetchall()]

    async def revoke(self, key_id: int) -> bool:
        async with transaction(self.conn):
            cur = await self.conn.execute(
                "UPDATE api_keys SET revoked_at = %s WHERE id = %s AND revoked_at IS NULL",
                (now_ms(), key_id),
            )
        return bool(cur.rowcount)


def _key(row: Row) -> ApiKey:
    return ApiKey(
        id=row["id"],
        name=row["name"],
        scopes=frozenset(json.loads(row["scopes"] or "[]")),
        owner_user_id=row["owner_user_id"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        revoked_at=row["revoked_at"],
    )
