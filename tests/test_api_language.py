"""The language API the web editor uses: /parse, /explain, /language, /commands (ADR-0011)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport

from doomtp_bot.api.app import create_app
from doomtp_bot.core.health import HealthRegistry
from doomtp_bot.modules import builtin_registry
from doomtp_bot.runtime.engine import Runtime
from doomtp_bot.storage.db import Databases
from tests.fakes import policy_with_channels

CHANNEL_ID, CHANNEL_LOGIN = "100", "doomtp"


@pytest.fixture
async def client(dbs: Databases) -> AsyncIterator[httpx.AsyncClient]:
    policy = await policy_with_channels(dbs.bot, (CHANNEL_ID, CHANNEL_LOGIN))
    runtime = Runtime(builtin_registry(), policy=policy, services={"policy": policy})
    app = create_app(HealthRegistry(), None, runtime=runtime, policy=policy)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


async def test_parse_returns_the_ast_and_invocation_spans(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/parse", json={"text": "random 1-6 | echo {1}", "context": "body"})
    body = response.json()
    assert response.status_code == 200 and body["ok"] is True
    assert body["ast"] == 'Pipe(random["1-6"], echo["{1}"])'
    assert [i["name"] for i in body["invocations"]] == ["random", "echo"]


async def test_parse_reports_the_error_with_its_column(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/parse", json={"text": "echo a ; b", "context": "body"})
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_RESERVED_OPERATOR"
    assert body["error"]["column"] == 8 and "reserved" in body["error"]["hint"]


async def test_parse_in_line_context_says_when_text_is_not_a_command(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/parse", json={"text": "just chatting", "context": "line", "channel": CHANNEL_LOGIN}
    )
    assert response.json() == {"ok": True, "syntax_version": "1.0", "not_a_command": True}


async def test_parse_uses_the_named_channels_prefix(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/parse", json={"text": "\U0001f3dc ping", "context": "line", "channel": CHANNEL_LOGIN}
    )
    assert response.json()["ast"] == "ping[]"


async def test_an_unknown_channel_is_a_404(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/parse", json={"text": "echo hi", "channel": "nobody"})
    assert response.status_code == 404


async def test_explain_returns_the_structured_report(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/explain", json={"text": "ping", "context": "body", "channel": CHANNEL_LOGIN}
    )
    body = response.json()
    assert body["ast"] == "ping[]"
    assert body["invocations"][0]["name"] == "ping" and body["invocations"][0]["allowed"] is True
    assert body["ran"] is False


async def test_explain_can_run_without_sending_anything(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/explain", json={"text": "ping", "context": "body", "channel": CHANNEL_LOGIN, "run": True}
    )
    body = response.json()
    assert body["ran"] is True and body["result"]["code"] == 0 and body["would_send"] == "pong"


async def test_language_describes_the_syntax_for_the_editor(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/v1/language")).json()
    assert body["syntax_version"] == "1.0"
    assert "||" in body["operators"] and "chatter" in body["roots"]
    assert "channel.chatter" in body["variable_namespaces"]
    assert body["roots_by_context"]["body"].count("arg") == 1
    assert body["error_codes"]["E_UNBALANCED_GROUP"] == "unbalanced parentheses"
    assert body["limits"]["MAX_INVOCATIONS"] == 8
    assert body["raw_tail_commands"]["explain"] == 1 and body["raw_tail_commands"]["cc add"] == 3


async def test_commands_lists_the_built_ins_with_their_usage(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/v1/commands")).json()
    by_name = {c["name"]: c for c in body["commands"]}
    assert by_name["ping"]["summary"]
    assert by_name["help"]["usage"] == "help [command]"
    assert by_name["role"]["required_role"] == "moderator"
    assert by_name["random"]["params"][0]["name"]


async def test_the_language_api_is_unavailable_without_a_runtime() -> None:
    app = create_app(HealthRegistry(), None)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        assert (await http.post("/api/v1/parse", json={"text": "echo hi"})).status_code == 503
