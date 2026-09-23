"""OAuth token persistence in the `bot` schema (oauth_tokens). The bot token uses identity 'bot'."""

from __future__ import annotations

import json
from dataclasses import dataclass

from doomtp_bot.clock import now_ms
from doomtp_bot.storage.db import Connection, Row, transaction

BOT_IDENTITY = "bot"


def broadcaster_identity(user_id: str) -> str:
    """A broadcaster's own token, stored per channel (ADR-0007, full tier)."""
    return f"broadcaster:{user_id}"


@dataclass(frozen=True, slots=True)
class StoredToken:
    identity: str
    user_id: str
    login: str
    access_token: str
    refresh_token: str | None
    scopes: tuple[str, ...]
    expires_at: int | None


class TokenStore:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    async def broadcasters(self) -> list[StoredToken]:
        """Every broadcaster token, to hand to the Twitch client when it starts."""
        async with await self.conn.execute(
            "SELECT * FROM oauth_tokens WHERE identity LIKE 'broadcaster:%'"
        ) as cur:
            return [_token(row) for row in await cur.fetchall()]

    async def forget(self, identity: str) -> bool:
        async with transaction(self.conn):
            cur = await self.conn.execute("DELETE FROM oauth_tokens WHERE identity = %s", (identity,))
        return bool(cur.rowcount)

    async def get(self, identity: str = BOT_IDENTITY) -> StoredToken | None:
        async with await self.conn.execute(
            "SELECT * FROM oauth_tokens WHERE identity = %s", (identity,)
        ) as cur:
            row = await cur.fetchone()
        return _token(row) if row is not None else None

    async def save(
        self,
        *,
        identity: str,
        user_id: str,
        login: str,
        access_token: str,
        refresh_token: str | None,
        scopes: list[str] | tuple[str, ...],
        expires_in: int | None,
    ) -> None:
        now = now_ms()
        expires_at = now + expires_in * 1000 if expires_in else None
        async with transaction(self.conn):
            await self.conn.execute(
                "INSERT INTO oauth_tokens (identity, user_id, login, access_token, refresh_token, scopes, expires_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (identity) DO UPDATE SET user_id = excluded.user_id, login = excluded.login,"
                " access_token = excluded.access_token, refresh_token = excluded.refresh_token, scopes = excluded.scopes,"
                " expires_at = excluded.expires_at, updated_at = excluded.updated_at",
                (
                    identity,
                    user_id,
                    login,
                    access_token,
                    refresh_token,
                    json.dumps(list(scopes)),
                    expires_at,
                    now,
                ),
            )

    async def update_refreshed(
        self, user_id: str, access_token: str, refresh_token: str, expires_in: int
    ) -> None:
        now = now_ms()
        async with transaction(self.conn):
            await self.conn.execute(
                "UPDATE oauth_tokens SET access_token = %s, refresh_token = %s, expires_at = %s, updated_at = %s WHERE user_id = %s",
                (access_token, refresh_token, now + expires_in * 1000, now, user_id),
            )


def _token(row: Row) -> StoredToken:
    return StoredToken(
        row["identity"],
        row["user_id"],
        row["login"],
        row["access_token"],
        row["refresh_token"],
        tuple(json.loads(row["scopes"])),
        row["expires_at"],
    )
