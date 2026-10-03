"""The hosts `http get` may fetch, their secrets and the limits: storage, `!admin http` and the audit (ADR-0020).

The API routes are in `tests/test_api_data.py`; the fetcher itself is in `tests/test_http.py`.
"""

from __future__ import annotations

import dataclasses
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest

from doomtp_bot.audit.log import read_audit
from doomtp_bot.lang.parser import Context
from doomtp_bot.modules import builtin_registry
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.runtime.engine import RunReport, Runtime
from doomtp_bot.runtime.result import Code
from doomtp_bot.storage.db import Databases, fetch_one
from doomtp_bot.webfetch.fetcher import HostRule, HttpError, HttpFetcher, HttpLimits, Secret
from doomtp_bot.webfetch.hosts import HostError, HostStore, check_secret, normalize_pattern
from tests.fakes import TickingClock, policy_with_channels

CHANNEL_ID, CHANNEL_LOGIN = "100", "doomtp"
USERS = {"owner": ("1", "owner", "Owner"), "alice": ("400", "alice", "Alice")}
KEY = Secret("query", "appid", "s3cret-value")


# ── names and secrets ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "pattern"),
    [("API.Example.com.", "api.example.com"), ("*.Example.com", "*.example.com"), (" wttr.in ", "wttr.in")],
)
def test_patterns_are_normalized(text: str, pattern: str) -> None:
    assert normalize_pattern(text) == pattern


@pytest.mark.parametrize(
    "text",
    ["localhost", "*.com", "127.0.0.1", "[::1]", "::1", "example.123", "-bad.example.com", "a b.com",
     "*.", "", "exa_mple.com", "http://example.com"],
)  # fmt: skip
def test_bad_patterns_are_refused(text: str) -> None:
    with pytest.raises(HostError):
        normalize_pattern(text)


@pytest.mark.parametrize(
    ("kind", "name", "value"),
    [("cookie", "x", "v"), ("header", "Host", "v"), ("header", "user-agent", "v"),
     ("header", "X Key", "v"), ("query", "a&b", "v"), ("query", "k", ""), ("query", "k", "has space"),
     ("query", "k", "x" * 513), ("header", "X-Key", "line\nbreak")],
)  # fmt: skip
def test_bad_secrets_are_refused(kind: str, name: str, value: str) -> None:
    with pytest.raises(HostError):
        check_secret(kind, name, value)


def test_good_secrets_pass() -> None:
    check_secret("query", "appid", "abc123")
    check_secret("header", "X-Api-Key", "abc-123_=")


# ── the store ─────────────────────────────────────────────────────────────────
async def test_hosts_round_trip_through_the_database(dbs: Databases) -> None:
    store = HostStore(dbs.bot)
    await store.reload()
    assert store.rules() == [] and store.limits() == HttpLimits(10, 60)

    await store.allow("API.OpenWeatherMap.org", actor="1", via="chat")
    await store.allow("wttr.in", plain_http=True, actor="1", via="chat")
    await store.set_secret("api.openweathermap.org", KEY, actor="1", via="api")
    await store.set_limits(channel_per_minute=3, actor="1", via="chat")

    fresh = HostStore(dbs.bot)  # what the bot sees after a restart
    await fresh.reload()
    assert fresh.rules() == [HostRule("api.openweathermap.org"), HostRule("wttr.in", plain_http=True)]
    assert fresh.secret_for("api.openweathermap.org") == KEY
    assert fresh.secret_for("wttr.in") is None
    assert fresh.limits() == HttpLimits(3, 60)

    # allowing again changes plain_http but keeps the secret
    entry = await fresh.allow("api.openweathermap.org", plain_http=True, actor="1", via="chat")
    assert entry.secret == KEY and entry.rule.plain_http

    assert await fresh.deny("api.openweathermap.org", actor="1", via="chat")
    assert not await fresh.deny("api.openweathermap.org", actor="1", via="chat")
    assert fresh.secret_for("api.openweathermap.org") is None
    assert await fetch_one(dbs.bot, "SELECT 1 FROM http_hosts WHERE pattern = 'api.openweathermap.org'") is None


async def test_a_secret_needs_a_listed_host_and_a_limit_a_sane_number(dbs: Databases) -> None:
    store = HostStore(dbs.bot)
    await store.reload()
    with pytest.raises(HostError, match="allow it first"):
        await store.set_secret("api.example.com", KEY, actor="1", via="api")
    await store.allow("api.example.com", actor="1", via="api")
    with pytest.raises(HostError):
        await store.set_secret("api.example.com", Secret("header", "Host", "x"), actor="1", via="api")
    with pytest.raises(HostError):
        await store.set_limits(host_per_minute=10_001, actor="1", via="api")
    assert store.limits() == HttpLimits(10, 60)


async def test_listings_and_the_audit_never_hold_the_value(dbs: Databases) -> None:
    store = HostStore(dbs.bot)
    await store.reload()
    await store.allow("api.example.com", actor="1", via="api")
    entry = await store.set_secret("api.example.com", KEY, actor="1", via="api")
    await store.set_secret("api.example.com", None, actor="1", via="api")
    await store.set_secret("api.example.com", KEY, actor="1", via="api")
    await store.deny("api.example.com", actor="1", via="api")

    assert entry.public()["secret"] == {"kind": "query", "name": "appid"}
    assert KEY.value not in repr(entry.public())
    rows = await read_audit(dbs.bot)
    assert [r["action"] for r in reversed(rows)] == [
        "http_hosts.allow", "http_hosts.secret", "http_hosts.secret", "http_hosts.secret", "http_hosts.deny",
    ]  # fmt: skip
    assert all(KEY.value not in repr(r) for r in rows)
    assert rows[1]["before"] is None and rows[1]["after"] == {"kind": "query", "name": "appid"}


async def test_the_fetcher_follows_the_stored_limits(dbs: Databases) -> None:
    store = HostStore(dbs.bot)
    await store.reload()
    await store.allow("api.example.com", actor="1", via="api")
    await store.set_limits(channel_per_minute=0, actor="1", via="api")
    with pytest.raises(HttpError) as caught:  # refused before anything goes out
        await HttpFetcher(store).get("c1", "https://api.example.com/x")
    assert caught.value.code == Code.UPSTREAM_LIMITED


# ── !admin http ───────────────────────────────────────────────────────────────
@dataclass
class Harness:
    hosts: HostStore
    policy: PolicyService
    runtime: Runtime

    async def run(self, who: str, text: str) -> RunReport:
        uid, name, display = USERS[who]
        channel = dataclasses.replace(self.policy.channel_info(CHANNEL_ID, CHANNEL_LOGIN), prefix="!")
        chatter = self.policy.build_chatter(CHANNEL_ID, uid, name, display, frozenset())
        ctx = self.runtime.make_context(channel=channel, invoker=chatter, context=Context.LINE, rng=random.Random(3))
        report = await self.runtime.run(text, ctx)
        assert report is not None
        return report


@pytest.fixture
async def h(dbs: Databases) -> AsyncIterator[Harness]:
    policy = await policy_with_channels(dbs.bot, bot_owner_ids=frozenset({"1"}), clock=TickingClock())
    hosts = HostStore(dbs.bot)
    await hosts.reload()
    runtime = Runtime(builtin_registry(), policy=policy, services={"policy": policy, "http_hosts": hosts})
    yield Harness(hosts, policy, runtime)


async def test_admin_http_edits_the_list(h: Harness) -> None:
    assert (await h.run("owner", "!admin http list")).send == (
        "hosts: none · limits: 10/min per channel, 60/min per host"
    )
    assert (await h.run("owner", "!admin http allow API.Example.com")).send == "http can fetch api.example.com"
    assert (await h.run("owner", "!admin http allow wttr.in http")).send == "http can fetch wttr.in (http too)"
    await h.hosts.set_secret("api.example.com", KEY, actor="1", via="api")
    listed = await h.run("owner", "!admin http list")
    assert KEY.value not in (listed.send or "") and KEY.value not in repr(listed.result)
    assert "appid" in (listed.send or "")

    assert (await h.run("owner", "!admin http secret api.example.com clear")).send == (
        "api.example.com has no secret now"
    )
    assert h.hosts.secret_for("api.example.com") is None
    assert (await h.run("owner", "!admin http deny wttr.in")).send == "http no longer fetches wttr.in"
    failed = await h.run("owner", "!admin http deny wttr.in")
    assert failed.result is not None and failed.result.code == Code.FAIL

    assert (await h.run("owner", "!admin http limit host 30")).send == "at most 30 a minute per host"
    assert (await h.run("owner", "!admin http limit")).send == "10/min per channel, 30/min per host"
    assert "needs a dot" in ((await h.run("owner", "!admin http allow localhost")).send or "")


async def test_chat_never_takes_a_secrets_value(h: Harness) -> None:
    await h.run("owner", "!admin http allow api.example.com")
    report = await h.run("owner", "!admin http secret api.example.com query appid s3cret-value")
    assert report.result is not None and not report.result.ok
    assert "PUT /api/v1/http-hosts/<host>/secret" in (report.send or "")
    assert h.hosts.secret_for("api.example.com") is None


async def test_only_the_bot_owner_edits_the_list(h: Harness) -> None:
    denied = await h.run("alice", "!admin http allow api.example.com")
    assert denied.result is not None and not denied.result.ok
    assert h.hosts.rules() == []
