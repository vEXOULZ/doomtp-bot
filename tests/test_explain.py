"""`!explain`: the report, and that `--run` changes nothing (spec §9)."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from doomtp_bot.customcmds.resolution import CustomCommandLoader
from doomtp_bot.customcmds.service import CustomCommandService
from doomtp_bot.lang.parser import Context
from doomtp_bot.modules import builtin_registry
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.runtime.engine import Runtime
from doomtp_bot.runtime.explain import ReportStore, explain
from doomtp_bot.runtime.result import Code
from doomtp_bot.runtime.variables import VarKey
from doomtp_bot.storage.db import Databases
from doomtp_bot.variables.access import VariableAccessPolicy
from doomtp_bot.variables.store import PostgresVariableStore
from tests.fakes import TickingClock, policy_with_channels

CHANNEL_ID, CHANNEL_LOGIN = "100", "doomtp"
USERS = {"mod": ("300", "mod", "Mod"), "alice": ("400", "alice", "Alice")}
BADGES = {"mod": {"moderator"}}


@dataclass
class Harness:
    policy: PolicyService
    runtime: Runtime
    store: PostgresVariableStore
    service: CustomCommandService

    def context(self, who: str) -> Any:
        channel = dataclasses.replace(self.policy.channel_info(CHANNEL_ID, CHANNEL_LOGIN), prefix="!")
        user = USERS[who]
        chatter = self.policy.build_chatter(
            CHANNEL_ID, user[0], user[1], user[2], frozenset(BADGES.get(who, set()))
        )
        return self.runtime.make_context(channel=channel, invoker=chatter)

    async def explain(self, who: str, text: str, **kw: Any) -> Any:
        return await explain(self.runtime, text, self.context(who), **kw)

    async def say(self, who: str, text: str) -> str | None:
        report = await self.runtime.run(text, self.context(who))
        assert report is not None
        return report.send


@pytest.fixture
async def h(dbs: Databases) -> AsyncIterator[Harness]:
    policy = await policy_with_channels(dbs.bot, (CHANNEL_ID, CHANNEL_LOGIN), clock=TickingClock())
    store = PostgresVariableStore(dbs.bot)
    access = VariableAccessPolicy(policy, dbs.bot)
    await access.reload()
    service = CustomCommandService(dbs.bot, on_grants_changed=access.reload)
    runtime = Runtime(
        builtin_registry(),
        policy=policy,
        store=store,
        access=access,
        custom=CustomCommandLoader(service),
        services={
            "policy": policy,
            "variable_store": store,
            "customcmds": service,
            "variable_access": access,
        },
    )
    yield Harness(policy, runtime, store, service)


async def test_it_reports_the_ast_and_each_invocation(h: Harness) -> None:
    report = await h.explain("alice", "!random 1-6 | echo you rolled {1}")
    assert report.ast == 'Pipe(random["1-6"], echo["you","rolled","{1}"])'
    assert [(i.index, i.name, i.allowed) for i in report.invocations] == [
        (1, "random", True),
        (2, "echo", True),
    ]
    assert report.failure is None
    assert "would run" in report.one_line()


async def test_it_names_the_first_failing_check(h: Harness) -> None:
    report = await h.explain("alice", "!role list")  # moderator only
    assert report.failure is not None and report.failure.code == Code.DENIED
    assert report.failed_index == 1
    assert [i.allowed for i in report.invocations] == [False]
    assert "requires moderator" in report.invocations[0].reason
    assert "would fail" in report.one_line()


async def test_it_reports_unknown_commands_and_parse_errors(h: Harness) -> None:
    unknown = await h.explain("alice", "!nope")
    assert unknown.invocations[0].reason == "unknown command"

    broken = await h.explain("alice", "!echo a ; b")
    assert "E_RESERVED_OPERATOR" in broken.parse_error
    assert broken.one_line().startswith("parse error:")


async def test_it_reports_placeholders_and_store_targets(h: Harness) -> None:
    report = await h.explain("mod", "!echo {chatter.display} > channel.note")
    assert report.stores == [{"variable": "channel.note", "append": False, "allowed": True}]
    assert report.invocations[0].placeholders[0]["reference"] == "{chatter.display}"

    viewer = await h.explain("alice", "!echo hi > channel.note")
    assert viewer.stores[0]["allowed"] is False
    assert "can't write channel.note" in viewer.one_line()  # the chat answer says so too (ADR-0010)
    assert "can't write" not in report.one_line()


async def test_it_shows_where_a_custom_command_came_from(h: Harness) -> None:
    created = await h.service.create(
        owner_user_id="400",
        owner_login="alice",
        name="hype",
        body="echo hyped",
        channel_id=CHANNEL_ID,
        prefix="!",
    )
    await h.service.publish(channel_id=CHANNEL_ID, name="hype", command=created, published_by=USERS["mod"][0])
    report = await h.explain("mod", "!hype")
    assert (report.invocations[0].source, report.invocations[0].owner) == ("publication", "alice")
    assert report.invocations[0].version == 1


async def test_run_evaluates_without_committing_or_sending(h: Harness) -> None:
    report = await h.explain("mod", "!echo 42 > channel.note", run=True)
    assert report.ran and report.run_result is not None and report.run_result.ok
    assert report.would_send == "42"  # what it *would* send; the caller sends nothing
    assert await h.store.get(VarKey("channel", CHANNEL_ID, name="note")) is not 42  # noqa: F632
    async with await h.store.conn.execute("SELECT COUNT(*) AS n FROM variables") as cur:
        assert (await cur.fetchone())["n"] == 0  # the write buffer was discarded (spec §9)


async def test_as_body_explains_in_the_body_context(h: Harness) -> None:
    line = await h.explain("alice", "!echo {arg.1}")
    assert line.failure is not None  # {arg.*} isn't available in a typed line

    body = await h.explain("alice", "echo {arg.1}", context=Context.BODY)
    assert body.failure is None and body.invocations[0].placeholders[0]["available"] is True


async def test_the_chat_command_answers_in_one_line(h: Harness) -> None:
    reply = await h.say("alice", "!explain !random 1-6 | echo {1}")
    assert reply is not None and reply.startswith("Pipe(random") and "1:random ✓" in reply

    ran = await h.say("alice", "!explain --run !ping")
    assert ran is not None and "would send: pong" in ran


async def test_the_chat_reply_links_the_full_report_only_when_chat_can_open_it(h: Harness) -> None:
    h.runtime.services["explain_reports"] = ReportStore()  # PUBLIC_WEB_UI off: no base URL
    reply = await h.say("alice", "!explain !ping")
    assert reply is not None and "full report" not in reply

    reports = ReportStore("https://bot.example/")
    h.runtime.services["explain_reports"] = reports
    reply = await h.say("alice", "!explain !random 1-6 | echo {1}")
    assert reply is not None and " — full report: https://bot.example/explain/" in reply
    kept = reports.get(reply.rsplit("/", 1)[1])
    assert kept is not None and kept["channel"] == CHANNEL_LOGIN
    assert [i["name"] for i in kept["invocations"]] == ["random", "echo"]


def test_kept_reports_expire_and_the_oldest_go_first() -> None:
    now = [0.0]
    reports = ReportStore("https://bot.example", ttl_s=60, limit=2, clock=lambda: now[0])
    first, second, third = (reports.keep({"n": n}) for n in range(3))
    assert reports.get(first) is None and reports.get(second) == {"n": 1}  # over the limit
    assert reports.link(third) == f"https://bot.example/explain/{third}"
    now[0] = 61
    assert reports.get(third) is None
    assert ReportStore().link(third) is None


async def test_a_line_without_the_command_sign_is_reported_as_chat(h: Harness) -> None:
    reply = await h.say("alice", "!explain ping")
    assert reply == "parse error: not a command: a line starts with the command sign !"
