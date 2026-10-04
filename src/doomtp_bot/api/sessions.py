"""Admin sessions for the web site (architecture §11, ADR-0016, ADR-0017).

The admin password is hashed with scrypt from the standard library and compared in constant time. It is
the way in that doesn't depend on Twitch; a Twitch sign-in makes the same kind of session. Sessions live
in memory: this is one process, and a restart logging everyone out is the right default. Without a
configured password, the password login is off rather than open.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network

SESSION_COOKIE = "doomtp_admin"
SESSION_TTL_S = 8 * 3600
SCRYPT = {"n": 2**14, "r": 8, "p": 1}
#: Where the password login is taken from unless ADMIN_PASSWORD_NETWORKS says otherwise: this host and
#: the private ranges. The password is the way in when Twitch is down, not a second front door.
LOCAL_NETWORKS = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"  # conventions:allow-infra

Networks = tuple[IPv4Network | IPv6Network, ...]


def parse_networks(csv: str) -> Networks | None:
    """ADMIN_PASSWORD_NETWORKS: comma-separated addresses or CIDRs, or `*` for anywhere (None).
    A typo raises at startup rather than quietly letting nobody, or everybody, in."""
    parts = [p.strip() for p in csv.split(",") if p.strip()]
    if "*" in parts:
        return None
    return tuple(ip_network(p, strict=False) for p in parts)


def address_in(address: str, networks: Networks | None) -> bool:
    """Whether an address is in `networks` (None: anywhere). An IPv4 address seen as IPv6
    (`::ffff:192.168.1.5`) counts as the IPv4 one."""  # conventions:allow-infra
    if networks is None:
        return True
    try:
        ip = ip_address(address)
    except ValueError:  # "unknown", or whatever a test transport calls itself
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in network for network in networks)


def hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    salt = salt or secrets.token_bytes(16)
    return salt, hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **SCRYPT)


@dataclass
class Session:
    token: str
    created_at: float
    csrf: str
    # Who is behind it (ADR-0017, ADR-0026). The password gives an admin session with no user; a Twitch
    # sign-in gives the signed-in user, as an admin (bot owner or bot admin), a moderator of `channels`
    # (their own among them, when the bot is there), or a user who manages none.
    role: str = "admin"  # "admin" | "moderator" | "user"
    user_id: str | None = None
    user_login: str | None = None
    channels: frozenset[str] | None = None  # logins a moderator manages; None means every channel
    # A Twitch sign-in's token (`twitch.signin.Grant`), and when its role and channels were last checked.
    grant: object | None = field(default=None, repr=False)
    checked_at: float = 0.0

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


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

    def login(
        self,
        *,
        role: str = "admin",
        user_id: str | None = None,
        user_login: str | None = None,
        channels: frozenset[str] | None = None,
        grant: object | None = None,
        checked_at: float = 0.0,
    ) -> Session:
        """A new session. Without arguments, the password's admin session."""
        if role not in ("admin", "moderator", "user"):
            raise ValueError(f"unknown session role {role}")
        if role == "moderator" and (user_id is None or not channels):
            raise ValueError("a moderator session needs the user and the channels they moderate")
        if role == "user":
            if user_id is None:
                raise ValueError("a user session needs the user")
            channels = frozenset()
        session = Session(
            secrets.token_urlsafe(32),
            self._now(),
            secrets.token_urlsafe(16),
            role=role,
            user_id=user_id,
            user_login=user_login,
            channels=frozenset(c.lower() for c in channels) if channels is not None else None,
            grant=grant,
            checked_at=checked_at,
        )
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

    The password is the only thing between the internet and an admin session once the site is public, and
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


@dataclass
class ReadLimiter:
    """Unauthenticated reads per client address (ADR-0026: the public chat log): at most `reads` inside
    `window_s`. Search is the costly part of the API that anyone can reach, so it is metered."""

    reads: int = 30
    window_s: float = 60.0
    clock: object = time.monotonic
    _hits: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque), repr=False)

    def _now(self) -> float:
        return float(self.clock())  # type: ignore[operator]

    def hit(self, address: str) -> int | None:
        """Count a read. Whole seconds to wait when `address` is over its limit (the read isn't counted
        then), else None."""
        now, hits = self._now(), self._hits[address]
        while hits and now - hits[0] >= self.window_s:
            hits.popleft()
        if len(hits) >= self.reads:
            return max(1, math.ceil(self.window_s - (now - hits[0])))
        hits.append(now)
        return None
