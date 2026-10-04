"""`http get`'s one code path to the outside (ADR-0020): a JSON GET to an allowed host, and nothing else.

Every request is checked before it leaves: the scheme and port, that the host is a name on the admin's
allow-list, and — through the connector's resolver — that every address the name resolves to is public.
The connection goes to exactly the addresses that were checked. Redirects are followed by hand, at most
three, and each hop is checked again. Rate limits and a short cache sit in front of all of it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import socket
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

import aiohttp
import structlog
from aiohttp.abc import AbstractResolver, ResolveResult
from yarl import URL

from doomtp_bot import __version__
from doomtp_bot.runtime.result import Code, ErrorCode
from doomtp_bot.webfetch.addresses import is_ip_literal, matching_pattern, refused

log = structlog.get_logger(__name__)

TIMEOUT_S = 3.0
MAX_BODY_BYTES = 128 * 1024
MAX_REDIRECTS = 3
CACHE_S = 60.0
CHANNEL_PER_MINUTE = 10
HOST_PER_MINUTE = 60
DEFAULT_PORTS = {"https": 443, "http": 80}
_REDIRECTS = frozenset({301, 302, 303, 307, 308})


class HttpError(Exception):
    """A refused or failed fetch. `error` names it (E_HTTP_*), or is empty for code 125 (rate limited)."""

    def __init__(self, error: str, message: str, code: int | None = None, **data: Any) -> None:
        super().__init__(message)
        self.error = error
        self.code = int(ErrorCode[error]) if code is None else code
        self.data: dict[str, Any] = ({"error": error} if error else {}) | data


@dataclass(frozen=True, slots=True)
class HostRule:
    """One allow-list entry. `plain_http` lets the host be fetched over http:// as well."""

    pattern: str
    plain_http: bool = False


@dataclass(frozen=True, slots=True)
class Secret:
    """A host's API key, attached to every request to it and never shown back (ADR-0020)."""

    kind: str  # "query" | "header"
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class HttpLimits:
    """Requests a minute: per channel, and per host across all channels (ADR-0020 §Limits)."""

    channel_per_minute: int
    host_per_minute: int

    def as_dict(self) -> dict[str, int]:
        return {"channel_per_minute": self.channel_per_minute, "host_per_minute": self.host_per_minute}


class HostPolicy(Protocol):
    """Where the allow-list, the secrets and the limits come from."""

    def rules(self) -> Iterable[HostRule]: ...

    def secret_for(self, pattern: str) -> Secret | None: ...

    def limits(self) -> HttpLimits | None: ...


@dataclass
class StaticHosts:
    """A fixed allow-list, for tests and for a bot that hasn't loaded the admin's list yet."""

    entries: tuple[HostRule, ...] = ()
    secrets: dict[str, Secret] = field(default_factory=dict)

    def rules(self) -> Iterable[HostRule]:
        return self.entries

    def secret_for(self, pattern: str) -> Secret | None:
        return self.secrets.get(pattern)

    def limits(self) -> HttpLimits | None:
        return None  # the fetcher's own


@dataclass(frozen=True, slots=True)
class Fetched:
    value: Any
    host: str
    status: int
    size: int
    cached: bool = False


class _Refused(OSError):
    pass


class CheckedResolver(AbstractResolver):
    """Resolves once, refuses the whole answer if any address in it is refused, and hands aiohttp only
    what was checked — so the connection can't go anywhere the check didn't see."""

    def __init__(self, inner: AbstractResolver, is_refused: Callable[[str], bool]) -> None:
        self.inner = inner
        self.is_refused = is_refused
        self.refused_address: str | None = None

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        answers = await self.inner.resolve(host, port, family)
        for answer in answers:
            if self.is_refused(answer["host"]):
                self.refused_address = answer["host"]
                raise _Refused(f"{host} resolves to a refused address")
        return answers

    async def close(self) -> None:
        await self.inner.close()


class _Window:
    """Requests in the last minute, per key."""

    def __init__(self, limit: int, clock: Callable[[], float]) -> None:
        self.limit = limit
        self.clock = clock
        self.hits: dict[str, deque[float]] = {}

    def full(self, key: str) -> bool:
        hits = self.hits.get(key)
        if hits is None:
            return self.limit <= 0  # a limit of 0 turns requests off
        cutoff = self.clock() - 60.0
        while hits and hits[0] <= cutoff:
            hits.popleft()
        return len(hits) >= self.limit

    def add(self, key: str) -> None:
        self.hits.setdefault(key, deque()).append(self.clock())


class HttpFetcher:
    def __init__(
        self,
        hosts: HostPolicy,
        *,
        resolver: Callable[[], AbstractResolver] | None = None,
        is_refused: Callable[[str], bool] = refused,
        ports: dict[str, int] | None = None,
        timeout_s: float = TIMEOUT_S,
        max_bytes: int = MAX_BODY_BYTES,
        cache_s: float = CACHE_S,
        channel_per_minute: int = CHANNEL_PER_MINUTE,
        host_per_minute: int = HOST_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.hosts = hosts
        self.resolver = resolver or aiohttp.ThreadedResolver
        self.is_refused = is_refused
        self.ports = ports or DEFAULT_PORTS
        self.timeout_s = timeout_s
        self.max_bytes = max_bytes
        self.cache_s = cache_s
        self.clock = clock
        self.per_channel = _Window(channel_per_minute, clock)
        self.per_host = _Window(host_per_minute, clock)
        self._cache: dict[str, tuple[float, Fetched]] = {}
        self._inflight: dict[str, asyncio.Future[Fetched]] = {}

    def check(self, url: URL) -> HostRule:
        """The allow-list entry for `url`, or E_HTTP_NOT_ALLOWED saying what's wrong with it."""
        host = url.raw_host
        if not url.absolute or host is None:
            raise HttpError("E_HTTP_NOT_ALLOWED", f"not a web address: {url.human_repr()}")
        if url.user is not None or url.password is not None:
            raise HttpError("E_HTTP_NOT_ALLOWED", "addresses with a user or password aren't fetched")
        if is_ip_literal(host):
            raise HttpError("E_HTTP_NOT_ALLOWED", "only named hosts are fetched, not addresses", host=host)
        rules = {rule.pattern: rule for rule in self.hosts.rules()}
        pattern = matching_pattern(host, rules)
        if pattern is None:
            raise HttpError("E_HTTP_NOT_ALLOWED", f"{host} isn't on the bot's list of hosts", host=host)
        rule = rules[pattern]
        if url.scheme not in self.ports or (url.scheme == "http" and not rule.plain_http):
            raise HttpError("E_HTTP_NOT_ALLOWED", f"{host} is fetched over https only", host=host)
        if url.port != self.ports[url.scheme]:
            raise HttpError("E_HTTP_NOT_ALLOWED", f"port {url.port} isn't allowed", host=host)
        return rule

    async def get(self, channel_id: str, raw_url: str) -> Fetched:
        try:
            url = URL(raw_url).with_fragment(None)
        except ValueError:
            raise HttpError("E_HTTP_NOT_ALLOWED", f"not a web address: {raw_url}") from None
        self.check(url)
        key = str(url)
        cached = self._cache.get(key)
        if cached is not None and cached[0] > self.clock():
            return dataclasses.replace(cached[1], cached=True)
        running = self._inflight.get(key)
        if running is not None:  # the same GET is on its way already: share it
            return await asyncio.shield(running)

        host = url.raw_host or ""
        limits = self.hosts.limits()
        if limits is not None:  # an admin may have changed them since the last request
            self.per_channel.limit, self.per_host.limit = limits.channel_per_minute, limits.host_per_minute
        if self.per_channel.full(channel_id):
            raise HttpError("", "too many web requests from this channel; try again in a minute",
                            code=Code.UPSTREAM_LIMITED, host=host)  # fmt: skip
        if self.per_host.full(host):
            raise HttpError("", f"too many requests to {host}; try again in a minute",
                            code=Code.UPSTREAM_LIMITED, host=host)  # fmt: skip
        self.per_channel.add(channel_id)
        self.per_host.add(host)

        future: asyncio.Future[Fetched] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            fetched = await self._fetch(url)
        except BaseException as exc:
            future.set_exception(exc)
            future.exception()  # retrieved here, so a future nobody else awaited doesn't warn
            raise
        else:
            future.set_result(fetched)
            self._cache[key] = (self.clock() + self.cache_s, fetched)
            self._prune()
            return fetched
        finally:
            del self._inflight[key]

    def _prune(self) -> None:
        now = self.clock()
        for key in [k for k, (expires, _) in self._cache.items() if expires <= now]:
            del self._cache[key]

    async def _fetch(self, url: URL) -> Fetched:
        try:
            async with asyncio.timeout(self.timeout_s):
                return await self._follow(url)
        except TimeoutError:
            raise HttpError("E_HTTP_TIMEOUT", f"{url.raw_host} didn't answer in time", host=url.raw_host) from None

    async def _follow(self, url: URL) -> Fetched:
        for _ in range(MAX_REDIRECTS + 1):
            rule = self.check(url)
            resolver = CheckedResolver(self.resolver(), self.is_refused)
            connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False, force_close=True)
            request_url, headers = self._with_secret(url, rule)
            try:
                async with (
                    aiohttp.ClientSession(
                        connector=connector,
                        cookie_jar=aiohttp.DummyCookieJar(),
                        headers={"User-Agent": f"doomtp-bot/{__version__}", "Accept": "application/json"},
                    ) as session,
                    session.get(request_url, headers=headers, allow_redirects=False) as response,
                ):
                    if response.status in _REDIRECTS and "Location" in response.headers:
                        url = url.join(URL(response.headers["Location"])).with_fragment(None)
                        continue
                    return await self._read(url, response)
            except aiohttp.ClientError as exc:
                if resolver.refused_address is not None:
                    raise HttpError(
                        "E_HTTP_ADDRESS",
                        f"{url.raw_host} points somewhere the bot doesn't go",
                        host=url.raw_host,
                    ) from None
                log.info("http.unreachable", host=url.raw_host, error=type(exc).__name__)
                raise HttpError("E_HTTP_UNREACHABLE", f"couldn't reach {url.raw_host}", host=url.raw_host) from None
        raise HttpError("E_HTTP_NOT_ALLOWED", f"more than {MAX_REDIRECTS} redirects", host=url.raw_host)

    def _with_secret(self, url: URL, rule: HostRule) -> tuple[URL, dict[str, str]]:
        """Only this hop's own host gets its secret, so a redirect elsewhere never carries it."""
        secret = self.hosts.secret_for(rule.pattern)
        if secret is None:
            return url, {}
        if secret.kind == "header":
            return url, {secret.name: secret.value}
        return url.update_query({secret.name: secret.value}), {}

    async def _read(self, url: URL, response: aiohttp.ClientResponse) -> Fetched:
        host = url.raw_host or ""
        if not 200 <= response.status < 300:
            raise HttpError("E_HTTP_STATUS", f"{host} answered {response.status}", host=host, status=response.status)
        if (response.content_length or 0) > self.max_bytes:
            raise HttpError("E_HTTP_TOO_BIG", f"{host}'s answer is over {self.max_bytes // 1024} KB", host=host)
        body = bytearray()
        async for chunk in response.content.iter_chunked(16 * 1024):
            body += chunk
            if len(body) > self.max_bytes:
                raise HttpError("E_HTTP_TOO_BIG", f"{host}'s answer is over {self.max_bytes // 1024} KB", host=host)
        try:
            value = json.loads(body.decode(response.get_encoding() if response.charset else "utf-8"))
        except (ValueError, LookupError):
            raise HttpError("E_HTTP_NOT_JSON", f"{host} didn't answer with JSON", host=host) from None
        return Fetched(value, host, response.status, len(body))
