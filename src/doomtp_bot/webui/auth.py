"""Admin session handling for the web UI (architecture §11).

A single local admin password guards `/admin`, hashed with scrypt from the standard library and compared
in constant time. Sessions live in memory: this is one process, and a restart logging admins out is the
right default for a LAN tool. Without a configured password, `/admin` is disabled rather than open.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field

SESSION_COOKIE = "doomtp_admin"
SESSION_TTL_S = 8 * 3600
SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    salt = salt or secrets.token_bytes(16)
    return salt, hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **SCRYPT)


@dataclass
class Session:
    token: str
    created_at: float
    csrf: str


@dataclass
class AdminAuth:
    """Password check plus session bookkeeping. `enabled` is False when no password is configured."""

    password: str | None = None
    ttl_s: float = SESSION_TTL_S
    clock: object = time.monotonic
    _salt: bytes = field(default=b"", repr=False)
    _digest: bytes = field(default=b"", repr=False)
    _sessions: dict[str, Session] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.password:
            self._salt, self._digest = hash_password(self.password)

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def _now(self) -> float:
        return float(self.clock())  # type: ignore[operator]

    def check_password(self, attempt: str) -> bool:
        if not self.enabled:
            return False
        _, digest = hash_password(attempt, self._salt)
        return hmac.compare_digest(digest, self._digest)

    def login(self) -> Session:
        session = Session(secrets.token_urlsafe(32), self._now(), secrets.token_urlsafe(16))
        self._sessions[session.token] = session
        return session

    def session(self, token: str | None) -> Session | None:
        if not token:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        if self._now() - session.created_at > self.ttl_s:
            self._sessions.pop(token, None)
            return None
        return session

    def logout(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)

    def valid_csrf(self, token: str | None, csrf: str | None) -> bool:
        session = self.session(token)
        return session is not None and bool(csrf) and hmac.compare_digest(session.csrf, csrf or "")
