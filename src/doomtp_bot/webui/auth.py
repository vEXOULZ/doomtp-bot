"""Admin session handling for the web UI (architecture §11).

A single local admin password guards `/admin`, hashed with scrypt from the standard library and compared
in constant time. Sessions live in memory: this is one process, and a restart logging admins out is the
right default for a LAN tool. Without a configured password, `/admin` is disabled rather than open.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import time
from collections import defaultdict, deque
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

    def expires_in(self, session: Session) -> float:
        """Seconds until `session` expires. The clock is monotonic, so callers add this to wall time."""
        return max(0.0, self.ttl_s - (self._now() - session.created_at))


@dataclass
class LoginLimiter:
    """Failed logins per client address: after `attempts` inside `window_s`, wait until the oldest ages out.

    The password is the only thing between the internet and `/admin` once the pages are published, and
    scrypt alone only slows a guesser down. A success clears the address's record.
    """

    attempts: int = 5
    window_s: float = 300.0
    clock: object = time.monotonic
    _failures: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque), repr=False)

    def _now(self) -> float:
        return float(self.clock())  # type: ignore[operator]

    def _recent(self, address: str) -> deque[float]:
        failures = self._failures[address]
        while failures and self._now() - failures[0] >= self.window_s:
            failures.popleft()
        return failures

    def retry_after(self, address: str) -> int | None:
        """Whole seconds to wait before `address` may try again, or None when it may try now."""
        failures = self._recent(address)
        if len(failures) < self.attempts:
            if not failures:
                self._failures.pop(address, None)
            return None
        return max(1, math.ceil(self.window_s - (self._now() - failures[0])))

    def failed(self, address: str) -> None:
        self._recent(address).append(self._now())

    def reset(self, address: str) -> None:
        self._failures.pop(address, None)
