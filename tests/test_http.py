"""`http get` (ADR-0020): who may call it, the address rules, redirects, limits, the cache and errors.

A local server stands in for the web. A fake resolver maps test names onto it, so the tests can say what
a name "resolves to" — including the addresses the bot must never reach.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult

from doomtp_bot.lang.parser import Context
from doomtp_bot.runtime.context import Publisher
from doomtp_bot.runtime.result import Code, ErrorCode
from doomtp_bot.webfetch.addresses import is_ip_literal, matching_pattern, refused
from doomtp_bot.webfetch.fetcher import HostRule, HttpError, HttpFetcher, Secret, StaticHosts
from tests.runtime.helpers import make_runtime, run

ADMIN = Publisher(id="admin1", login="boss", command_id="cc1", command_name="weather")
NOBODY = Publisher(id="u9", login="someone", command_id="cc2", command_name="weather")


# ── address rules ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # CGNAT
        "0.0.0.0",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "::",
        "fe80::1%eth0",
        "fc00::1",
        "::ffff:127.0.0.1",  # IPv4-mapped
        "::ffff:10.0.0.1",
        "64:ff9b::a00:1",  # NAT64 of 10.0.0.1
        "2002:7f00:1::1",  # 6to4
        "2001:0:4136:e378::1",  # Teredo
        "not an address",
    ],
)
def test_refused_addresses(address: str) -> None:
    assert refused(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public_addresses_pass(address: str) -> None:
    assert not refused(address)


def test_host_patterns() -> None:
    patterns = ["api.example.com", "*.weather.test"]
    assert matching_pattern("API.example.com.", patterns) == "api.example.com"
    assert matching_pattern("eu.weather.test", patterns) == "*.weather.test"
    assert matching_pattern("a.b.weather.test", patterns) == "*.weather.test"
    assert matching_pattern("weather.test", patterns) is None  # the wildcard is for subdomains only
    assert matching_pattern("evilweather.test", patterns) is None
    assert matching_pattern("example.com", patterns) is None
    assert is_ip_literal("[::1]") and is_ip_literal("127.0.0.1") and not is_ip_literal("api.test")


# ── a local web ───────────────────────────────────────────────────────────────
@dataclass
class Web:
    port: int
    hits: list[str] = field(default_factory=list)
    seen: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture
async def site() -> AsyncIterator[Web]:
    state = Web(0)

    async def handle(request: web.Request) -> web.StreamResponse:
        state.hits.append(request.path)
        state.seen.append({"query": dict(request.query), "headers": dict(request.headers),
                           "host": request.host})  # fmt: skip
        path = request.path
        if path == "/json":
            return web.json_response({"current": {"temp_c": 12}, "list": [{"name": "a"}, {"name": "b"}]})
        if path == "/big":
            return web.json_response({"x": "y" * 200_000})
        if path == "/text":
            return web.Response(text="hello")
        if path == "/fail":
            return web.json_response({"error": "nope"}, status=503)
        if path == "/slow":
            await asyncio.sleep(2)
            return web.json_response({})
        if path == "/hop":
            raise web.HTTPFound("/json")
        if path == "/loop":
            raise web.HTTPFound("/loop")
        if path == "/to-other":
            raise web.HTTPFound(f"http://other.test:{state.port}/json")
        if path == "/to-private":
            raise web.HTTPFound(f"http://private.test:{state.port}/json")
        if path == "/to-literal":
            raise web.HTTPFound(f"http://127.0.0.1:{state.port}/json")
        return web.Response(status=404)

    app = web.Application()
    app.router.add_route("GET", "/{tail:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    server = web.TCPSite(runner, "127.0.0.1", 0)
    await server.start()
    state.port = runner.addresses[0][1]
    yield state
    await runner.cleanup()


class FakeResolver(AbstractResolver):
    """Test names resolve to the local server; `private.test` stands for a name pointing inside."""

    NAMES = {
        "api.test": ["127.0.0.1"],
        "other.test": ["127.0.0.1"],
        "private.test": ["10.0.0.5"],
        "mixed.test": ["8.8.8.8", "127.0.0.1"],
        "inside.test": ["127.0.0.1"],
    }

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        if host not in self.NAMES:
            raise OSError(f"no such name {host}")
        return [
            ResolveResult(
                hostname=host, host=ip, port=port, family=socket.AF_INET, proto=0, flags=socket.AI_NUMERICHOST
            )  # fmt: skip
            for ip in self.NAMES[host]
        ]

    async def close(self) -> None:
        pass


LOCAL = {"127.0.0.1"}  # the test server: the one address the "public" tests may reach


def fetcher(
    web_: Web,
    *hosts: str,
    is_refused: Callable[[str], bool] = lambda a: a not in LOCAL and refused(a),
    secrets: dict[str, Secret] | None = None,
    **kwargs: Any,
) -> HttpFetcher:
    rules = tuple(HostRule(h, plain_http=True) for h in (hosts or ("api.test",)))
    return HttpFetcher(
        StaticHosts(rules, secrets or {}),
        resolver=FakeResolver,
        is_refused=is_refused,
        ports={"https": 443, "http": web_.port},
        **kwargs,
    )


async def fails(f: HttpFetcher, url: str, error: str, channel: str = "c1") -> HttpError:
    with pytest.raises(HttpError) as caught:
        await f.get(channel, url)
    assert caught.value.error == error, caught.value
    if error:
        assert caught.value.code == ErrorCode[error]
    return caught.value


# ── the fetcher ───────────────────────────────────────────────────────────────
async def test_fetches_json(site: Web) -> None:
    got = await fetcher(site).get("c1", f"http://api.test:{site.port}/json")
    assert got.value["current"] == {"temp_c": 12} and got.status == 200 and not got.cached


async def test_host_must_be_on_the_list(site: Web) -> None:
    await fails(fetcher(site), f"http://other.test:{site.port}/json", "E_HTTP_NOT_ALLOWED")
    assert site.hits == []


async def test_ip_literals_are_refused_even_when_listed(site: Web) -> None:
    f = fetcher(site, "api.test", "127.0.0.1")
    await fails(f, f"http://127.0.0.1:{site.port}/json", "E_HTTP_NOT_ALLOWED")
    await fails(f, f"http://[::1]:{site.port}/json", "E_HTTP_NOT_ALLOWED")
    assert site.hits == []


async def test_scheme_port_and_credentials(site: Web) -> None:
    https_only = HttpFetcher(StaticHosts((HostRule("api.test"),)), resolver=FakeResolver,
                             ports={"https": 443, "http": site.port})  # fmt: skip
    await fails(https_only, f"http://api.test:{site.port}/json", "E_HTTP_NOT_ALLOWED")
    f = fetcher(site)
    await fails(f, "http://api.test:8080/json", "E_HTTP_NOT_ALLOWED")
    await fails(f, f"ftp://api.test:{site.port}/json", "E_HTTP_NOT_ALLOWED")
    await fails(f, f"http://user:pw@api.test:{site.port}/json", "E_HTTP_NOT_ALLOWED")
    await fails(f, "not a url", "E_HTTP_NOT_ALLOWED")
    assert site.hits == []


async def test_a_name_pointing_inside_is_refused(site: Web) -> None:
    """The real address rules: the allowed name resolves to loopback, so nothing is sent."""
    f = fetcher(site, "inside.test", "mixed.test", is_refused=refused)
    await fails(f, f"http://inside.test:{site.port}/json", "E_HTTP_ADDRESS")
    # one bad answer among good ones refuses the lot: the connector might have picked either
    await fails(f, f"http://mixed.test:{site.port}/json", "E_HTTP_ADDRESS")
    assert site.hits == []


async def test_redirects_are_checked_at_every_hop(site: Web) -> None:
    f = fetcher(site, "api.test", "private.test")
    got = await f.get("c1", f"http://api.test:{site.port}/hop")
    assert got.value["current"]["temp_c"] == 12 and site.hits == ["/hop", "/json"]
    site.hits.clear()
    await fails(f, f"http://api.test:{site.port}/to-other", "E_HTTP_NOT_ALLOWED")  # not on the list
    await fails(f, f"http://api.test:{site.port}/to-literal", "E_HTTP_NOT_ALLOWED")  # an address
    await fails(f, f"http://api.test:{site.port}/to-private", "E_HTTP_ADDRESS")  # a name for 10.0.0.5
    assert site.hits == ["/to-other", "/to-literal", "/to-private"]  # no hop was followed
    await fails(f, f"http://api.test:{site.port}/loop", "E_HTTP_NOT_ALLOWED")
    assert site.hits.count("/loop") == 4  # the first request and three redirects


async def test_a_secret_goes_only_to_its_own_host(site: Web) -> None:
    f = fetcher(site, "api.test", "other.test", secrets={"api.test": Secret("header", "X-Key", "s3cret")})
    await f.get("c1", f"http://api.test:{site.port}/to-other")
    assert site.seen[0]["headers"].get("X-Key") == "s3cret"
    assert "X-Key" not in site.seen[1]["headers"]
    q = fetcher(site, secrets={"api.test": Secret("query", "key", "s3cret")})
    await q.get("c1", f"http://api.test:{site.port}/json?q=lisbon")
    assert site.seen[2]["query"] == {"q": "lisbon", "key": "s3cret"}
    assert "Cookie" not in site.seen[2]["headers"]
    assert site.seen[2]["headers"]["User-Agent"].startswith("doomtp-bot/")


async def test_failures_have_their_own_codes(site: Web) -> None:
    f = fetcher(site, timeout_s=0.3)
    await fails(f, f"http://api.test:{site.port}/big", "E_HTTP_TOO_BIG")
    await fails(f, f"http://api.test:{site.port}/text", "E_HTTP_NOT_JSON")
    status = await fails(f, f"http://api.test:{site.port}/fail", "E_HTTP_STATUS")
    assert status.data["status"] == 503
    await fails(f, f"http://api.test:{site.port}/slow", "E_HTTP_TIMEOUT")


async def test_a_connection_that_fails_is_unreachable(site: Web) -> None:
    with socket.socket() as spare:  # a port nothing listens on
        spare.bind(("127.0.0.1", 0))
        closed = spare.getsockname()[1]
    f = HttpFetcher(StaticHosts((HostRule("api.test", plain_http=True),)), resolver=FakeResolver,
                    is_refused=lambda a: False, ports={"https": 443, "http": closed})  # fmt: skip
    await fails(f, f"http://api.test:{closed}/json", "E_HTTP_UNREACHABLE")


async def test_identical_gets_share_one_response(site: Web) -> None:
    now = [0.0]
    f = fetcher(site, clock=lambda: now[0])
    url = f"http://api.test:{site.port}/json"
    first, second = await asyncio.gather(f.get("c1", url), f.get("c2", url))
    again = await f.get("c1", url)
    assert site.hits == ["/json"] and again.cached and first.value == second.value
    now[0] = 61.0
    await f.get("c1", url)
    assert site.hits == ["/json", "/json"]


async def test_rate_limits(site: Web) -> None:
    now = [0.0]
    f = fetcher(site, clock=lambda: now[0], channel_per_minute=2, host_per_minute=3, cache_s=0)
    url = f"http://api.test:{site.port}/json"
    await f.get("c1", url)
    await f.get("c1", url + "?n=2")
    limited = await fails(f, url + "?n=3", "", channel="c1")
    assert limited.code == Code.UPSTREAM_LIMITED
    await f.get("c2", url + "?n=4")  # another channel, same host: the host's third
    await fails(f, url + "?n=5", "", channel="c3")  # the host is full now
    now[0] = 60.5
    await f.get("c1", url + "?n=6")
    assert len(site.hits) == 4


# ── the command ───────────────────────────────────────────────────────────────
class Admins:
    def is_bot_admin(self, user_id: str) -> bool:
        return user_id == ADMIN.id


class Writer:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def command_run(self, **row: Any) -> None:
        self.rows.append(row)


def runtime(f: HttpFetcher, writer: Writer | None = None) -> Any:
    return make_runtime(services={"policy": Admins(), "http": f, "chatlog_writer": writer or Writer()})


async def test_http_runs_only_in_an_admins_command(site: Web) -> None:
    rt = runtime(fetcher(site))
    url = f"http://api.test:{site.port}/json"
    typed = await run(rt, f"!http get {url}")
    assert typed.result.code == ErrorCode.E_HTTP_NOT_ALLOWED
    theirs = await run(rt, f"http get {url}", context=Context.BODY, publisher=NOBODY)
    assert theirs.result.code == ErrorCode.E_HTTP_NOT_ALLOWED
    trigger = await run(rt, f"http get {url}", context=Context.TRIGGER, publisher=ADMIN)
    assert trigger.result.code == ErrorCode.E_HTTP_NOT_ALLOWED
    assert site.hits == []
    ok = await run(rt, f"http get {url} [current][temp_c] | echo {{_1}}°C", context=Context.BODY,
                   publisher=ADMIN)  # fmt: skip
    assert ok.result.ok and ok.send == "12°C"


async def test_the_path_picks_one_value(site: Web) -> None:
    rt = runtime(fetcher(site))
    url = f"http://api.test:{site.port}/json"
    last = await run(rt, f"http get {url} [list][-1][name]", context=Context.BODY, publisher=ADMIN)
    assert last.result.data == "b"
    missing = await run(rt, f"http get {url} [current][wind]", context=Context.BODY, publisher=ADMIN)
    assert missing.result.code == ErrorCode.E_HTTP_PATH and "[current][wind]" in (missing.send or "")


async def test_each_request_is_logged_without_its_query(site: Web) -> None:
    writer = Writer()
    rt = runtime(fetcher(site, secrets={"api.test": Secret("query", "key", "s3cret")}), writer)
    await run(rt, f"http get http://api.test:{site.port}/json?q=secret-place", context=Context.BODY,
              publisher=ADMIN)  # fmt: skip
    await run(rt, f"http get http://api.test:{site.port}/text", context=Context.BODY, publisher=ADMIN)
    assert [r["expr"] for r in writer.rows] == ["GET http://api.test/json", "GET http://api.test/text"]
    assert [r["code"] for r in writer.rows] == [0, ErrorCode.E_HTTP_NOT_JSON]
    assert all(r["trigger_type"] == "http" for r in writer.rows)
    logged = json.dumps(writer.rows)
    assert "secret-place" not in logged and "s3cret" not in logged
