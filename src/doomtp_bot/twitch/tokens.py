"""OAuth token persistence in bot.db (oauth_tokens). The bot token uses identity 'bot'."""

from __future__ import annotations

import json
from dataclasses import dataclass

import aiosqlite

from doomtp_bot.clock import now_ms
from doomtp_bot.storage.db import transaction

BOT_IDENTITY = "bot"


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
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    async def get(self, identity: str = BOT_IDENTITY) -> StoredToken | None:
        async with self.conn.execute("SELECT * FROM oauth_tokens WHERE identity = ?", (identity,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return StoredToken(
            row["identity"],
            row["user_id"],
            row["login"],
            row["access_token"],
            row["refresh_token"],
            tuple(json.loads(row["scopes"])),
            row["expires_at"],
        )

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
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
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
                "UPDATE oauth_tokens SET access_token = ?, refresh_token = ?, expires_at = ?, updated_at = ? WHERE user_id = ?",
                (access_token, refresh_token, now + expires_in * 1000, now, user_id),
            )
