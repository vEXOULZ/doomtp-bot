"""`/api/v1`: keys, scopes, and the endpoints that read and change configuration (architecture §11)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from doomtp_bot.api.app import create_app
from doomtp_bot.api.keys import ApiKeyService
from doomtp_bot.clock import now_ms
from doomtp_bot.core.channels import ChannelManager
from doomtp_bot.core.health import ComponentHealth, HealthRegistry, Status
from doomtp_bot.customcmds.packs import PackService
from doomtp_bot.customcmds.resolution import CustomCommandLoader
from doomtp_bot.customcmds.service import CustomCommandService
from doomtp_bot.filters.service import FilterService
from doomtp_bot.modules import builtin_registry
from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.runtime.engine import Runtime
from doomtp_bot.storage.db import Databases
from doomtp_bot.triggers.service import TriggerService
from doomtp_bot.variables.store import PostgresVariableStore
from doomtp_bot.webui.auth import SESSION_COOKIE
from tests.fakes import policy_with_channels

CHANNEL_ID, CHANNEL_LOGIN = "100", "doomtp"
PASSWORD = "correct horse battery staple"
USERS = {"doomtp": CHANNEL_ID, "friend": "200", "alice": "400", "mod": "300"}


class FakeTwitch:
    bot_id = "999"

    def __init__(self) -> None:
        self.subscribed: list[str] = []

    async def subscribe_channel(self, channel_id: str) -> list[str]:
        self.subscribed.append(channel_id)
        return []

    async def unsubscribe_channel(self, channel_id: str) -> None:
        self.subscribed.remove(channel_id)

    async def resolve_user(self, login: str) -> dict[str, str] | None:
        uid = USERS.get(login.lower().lstrip("@"))
        return {"id": uid, "name": login.lower(), "display": login.title()} if uid else None

    async def login_for(self, user_id: str) -> str | None:
        return next((login for login, uid in USERS.items() if uid == user_id), None)


class FakeSessions:
    async def start_session(self, channel_id: str) -> None: ...

    async def end_session(self, channel_id: str, reason: str) -> None: ...


async def _ok_check() -> ComponentHealth:
    return ComponentHealth(Status.OK, {})


@pytest.fixture
async def app_and_keys(dbs: Databases) -> AsyncIterator[tuple[Any, ApiKeyService]]:
    policy = await policy_with_channels(dbs.bot, (CHANNEL_ID, CHANNEL_LOGIN))
    filters = FilterService(dbs.bot)
    await filters.reload()
    customcmds = CustomCommandService(dbs.bot, filters=filters)
    triggers = TriggerService(dbs.bot, filters=filters)
    await triggers.reload()
    packs = PackService(dbs.bot, customcmds)
    runtime = Runtime(
        builtin_registry(),
        policy=policy,
        custom=CustomCommandLoader(customcmds, packs),
        services={"policy": policy, "customcmds": customcmds, "packs": packs},
    )
    triggers.parser_params = runtime.parser_params
    health = HealthRegistry()
    health.register("databases", _ok_check)
    twitch = FakeTwitch()
    keys = ApiKeyService(dbs.bot)
    app = create_app(
        health,
        None,
        runtime=runtime,
        policy=policy,
        services={
            "customcmds": customcmds,
            "packs": packs,
            "triggers": triggers,
            "filters": filters,
            "health": health,
            "channels": ChannelManager(policy, twitch, FakeSessions()),
            "twitch": twitch,
            "variable_store": PostgresVariableStore(dbs.bot),
            "bot_db": dbs.bot,
            "chatlog_db": dbs.chatlog,
            "api_keys": keys,
        },
        admin_password=PASSWORD,
    )
    app.state.customcmds_service = customcmds
    app.state.chatlog = dbs.chatlog
    yield app, keys


@pytest.fixture
async def client(app_and_keys: tuple[Any, ApiKeyService]) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app_and_keys[0]), base_url="http://test"
    ) as http:
        yield http


@pytest.fixture
async def write_key(app_and_keys: tuple[Any, ApiKeyService]) -> str:
    _, secret = await app_and_keys[1].create(name="tests", scopes=("read", "write"))
    return secret


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# ── keys and scopes ────────────────────────────────────────────────────────
async def test_without_credentials_nothing_private_is_readable(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/channels")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert (await client.get("/api/v1/channels", headers=auth("dtb_nonsense"))).status_code == 401


async def test_a_read_key_cannot_write(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService]
) -> None:
    _, secret = await app_and_keys[1].create(name="reader")
    assert (await client.get("/api/v1/channels", headers=auth(secret))).status_code == 200
    refused = await client.patch(
        f"/api/v1/channels/{CHANNEL_LOGIN}", json={"quiet_errors": True}, headers=auth(secret)
    )
    assert refused.status_code == 403


async def test_a_revoked_key_stops_working(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService]
) -> None:
    keys = app_and_keys[1]
    key, secret = await keys.create(name="temporary")
    assert (await client.get("/api/v1/channels", headers=auth(secret))).status_code == 200
    assert await keys.revoke(key.id) is True
    assert (await client.get("/api/v1/channels", headers=auth(secret))).status_code == 401
    assert [k.name for k in await keys.list()] == []


async def test_a_key_records_when_it_was_used(app_and_keys: tuple[Any, ApiKeyService]) -> None:
    keys = app_and_keys[1]
    key, secret = await keys.create(name="watched")
    assert (await keys.list())[0].last_used_at is None
    assert await keys.verify(secret) is not None
    assert (await keys.list())[0].last_used_at is not None
    assert await keys.verify("dtb_not-a-key") is None
    assert key.scopes == frozenset({"read"})


async def test_a_session_write_needs_the_csrf_header(client: httpx.AsyncClient) -> None:
    login = await client.post("/admin/login", data={"password": PASSWORD})
    assert login.status_code == 303
    token = client.cookies.get(SESSION_COOKIE)
    assert token is not None

    assert (await client.get("/api/v1/channels")).status_code == 200  # the cookie reads fine
    refused = await client.patch(f"/api/v1/channels/{CHANNEL_LOGIN}", json={"quiet_errors": True})
    assert refused.status_code == 403

    page = (await client.get("/admin")).text
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    allowed = await client.patch(
        f"/api/v1/channels/{CHANNEL_LOGIN}", json={"quiet_errors": True}, headers={"X-CSRF-Token": csrf}
    )
    assert allowed.status_code == 200 and allowed.json()["quiet_errors"] is True


async def test_the_admin_page_creates_and_revokes_keys(client: httpx.AsyncClient) -> None:
    await client.post("/admin/login", data={"password": PASSWORD})
    csrf = (await client.get("/admin")).text.split('name="csrf" value="')[1].split('"')[0]
    created = await client.post(
        "/admin/keys", data={"name": "dashboard", "scopes": "read,write", "csrf": csrf}
    )
    assert created.status_code == 200 and "dtb_" in created.text
    secret = created.text.split("<code>dtb_")[1].split("</code>")[0]
    assert (await client.get("/api/v1/channels", headers=auth("dtb_" + secret))).status_code == 200

    # Not a hard-coded 1: Postgres sequences are not rolled back with the transaction that used them,
    # so ids carry across tests. Ask the page which key it is showing.
    key_id = (await client.get("/admin")).text.split('name="key_id" value="')[1].split('"')[0]
    revoked = await client.post("/admin/keys/revoke", data={"key_id": key_id, "csrf": csrf})
    assert revoked.status_code == 303
    assert (await client.get("/api/v1/channels", headers=auth("dtb_" + secret))).status_code == 401


# ── channels ───────────────────────────────────────────────────────────────
async def test_channels_are_listed_and_changed(client: httpx.AsyncClient, write_key: str) -> None:
    listing = (await client.get("/api/v1/channels", headers=auth(write_key))).json()
    assert [c["login"] for c in listing["channels"]] == [CHANNEL_LOGIN]

    patched = await client.patch(
        f"/api/v1/channels/{CHANNEL_LOGIN}",
        json={"prefix": "!", "automod_action": "timeout", "automod_timeout_s": 90},
        headers=auth(write_key),
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["prefix"] == "!" and body["automod"] == {"action": "timeout", "timeout_s": 90}

    empty = await client.patch(f"/api/v1/channels/{CHANNEL_LOGIN}", json={}, headers=auth(write_key))
    assert empty.status_code == 400
    assert (await client.get("/api/v1/channels/nobody", headers=auth(write_key))).status_code == 404


async def test_joining_and_parting_a_channel(client: httpx.AsyncClient, write_key: str) -> None:
    joined = await client.post("/api/v1/channels", json={"login": "friend"}, headers=auth(write_key))
    assert joined.status_code == 201 and joined.json()["channel_id"] == "200"
    assert (await client.get("/api/v1/channels/friend", headers=auth(write_key))).json()["active"] is True

    parted = await client.delete("/api/v1/channels/friend", headers=auth(write_key))
    assert parted.status_code == 200
    assert (await client.get("/api/v1/channels/friend", headers=auth(write_key))).json()["status"] == "parted"

    missing = await client.post("/api/v1/channels", json={"login": "ghost"}, headers=auth(write_key))
    assert missing.status_code == 404


async def test_a_banned_channel_needs_rejoin(
    client: httpx.AsyncClient, write_key: str, app_and_keys: tuple[Any, ApiKeyService]
) -> None:
    await client.post("/api/v1/channels", json={"login": "friend"}, headers=auth(write_key))
    await app_and_keys[0].state.channels.leave_banned("200")
    flagged = (await client.get("/api/v1/channels/friend", headers=auth(write_key))).json()
    assert flagged["status"] == "banned" and flagged["banned"] is True and flagged["active"] is False

    refused = await client.post("/api/v1/channels", json={"login": "friend"}, headers=auth(write_key))
    assert refused.status_code == 409 and "rejoin" in refused.json()["detail"]
    back = await client.post(
        "/api/v1/channels", json={"login": "friend", "rejoin": True}, headers=auth(write_key)
    )
    assert back.status_code == 201
    assert (await client.get("/api/v1/channels/friend", headers=auth(write_key))).json()["banned"] is False


async def test_modules_and_commands_are_toggled_through_the_same_services(
    client: httpx.AsyncClient, write_key: str
) -> None:
    off = await client.put(
        f"/api/v1/channels/{CHANNEL_LOGIN}/modules/basic", json={"enabled": False}, headers=auth(write_key)
    )
    assert off.status_code == 200
    commands = (await client.get(f"/api/v1/channels/{CHANNEL_LOGIN}/commands")).json()["commands"]
    assert {c["name"]: c["enabled"] for c in commands}["ping"] is False

    ruled = await client.patch(
        f"/api/v1/channels/{CHANNEL_LOGIN}/commands/ping",
        json={"required_role": "moderator"},
        headers=auth(write_key),
    )
    assert ruled.status_code == 200 and ruled.json()["required_role"] == "moderator"

    bad_role = await client.patch(
        f"/api/v1/channels/{CHANNEL_LOGIN}/commands/ping",
        json={"required_role": "wizard"},
        headers=auth(write_key),
    )
    assert bad_role.status_code == 400
    unknown = await client.patch(
        f"/api/v1/channels/{CHANNEL_LOGIN}/commands/nope", json={"enabled": False}, headers=auth(write_key)
    )
    assert unknown.status_code == 404


# ── filters and triggers ───────────────────────────────────────────────────
async def test_filters_round_trip(client: httpx.AsyncClient, write_key: str) -> None:
    added = await client.post(
        f"/api/v1/channels/{CHANNEL_LOGIN}/filters",
        json={"pattern": "badword", "kind": "word", "action": "block"},
        headers=auth(write_key),
    )
    assert added.status_code == 201
    entry_id = added.json()["id"]

    listed = (await client.get(f"/api/v1/channels/{CHANNEL_LOGIN}/filters", headers=auth(write_key))).json()
    assert [f["pattern"] for f in listed["filters"]] == ["badword"]

    disabled = await client.patch(
        f"/api/v1/channels/{CHANNEL_LOGIN}/filters/{entry_id}",
        json={"enabled": False},
        headers=auth(write_key),
    )
    assert disabled.status_code == 200

    removed = await client.delete(
        f"/api/v1/channels/{CHANNEL_LOGIN}/filters/{entry_id}", headers=auth(write_key)
    )
    assert removed.status_code == 200
    assert (
        await client.delete(f"/api/v1/channels/{CHANNEL_LOGIN}/filters/{entry_id}", headers=auth(write_key))
    ).status_code == 404

    bad = await client.post(
        f"/api/v1/channels/{CHANNEL_LOGIN}/filters",
        json={"pattern": "(unclosed", "kind": "regex"},
        headers=auth(write_key),
    )
    assert bad.status_code == 400


async def test_triggers_round_trip(client: httpx.AsyncClient, write_key: str) -> None:
    added = await client.post(
        f"/api/v1/channels/{CHANNEL_LOGIN}/triggers",
        json={"type": "cron", "expr": "echo hello", "schedule": {"cron": "0 18 * * fri"}},
        headers=auth(write_key),
    )
    assert added.status_code == 201
    trigger_id = added.json()["id"]

    listed = (await client.get(f"/api/v1/channels/{CHANNEL_LOGIN}/triggers", headers=auth(write_key))).json()
    assert [t["type"] for t in listed["triggers"]] == ["cron"]

    off = await client.patch(
        f"/api/v1/channels/{CHANNEL_LOGIN}/triggers/{trigger_id}",
        json={"enabled": False},
        headers=auth(write_key),
    )
    assert off.status_code == 200

    bad_cron = await client.post(
        f"/api/v1/channels/{CHANNEL_LOGIN}/triggers",
        json={"type": "cron", "expr": "echo hello", "schedule": {"cron": "every friday"}},
        headers=auth(write_key),
    )
    assert bad_cron.status_code == 400

    gone = await client.delete(
        f"/api/v1/channels/{CHANNEL_LOGIN}/triggers/{trigger_id}", headers=auth(write_key)
    )
    assert gone.status_code == 200


async def test_filter_and_trigger_writes_are_audited_as_api(
    client: httpx.AsyncClient, write_key: str
) -> None:
    base = f"/api/v1/channels/{CHANNEL_LOGIN}"
    entry_id = (
        await client.post(f"{base}/filters", json={"pattern": "badword"}, headers=auth(write_key))
    ).json()["id"]
    await client.patch(f"{base}/filters/{entry_id}", json={"enabled": False}, headers=auth(write_key))
    await client.delete(f"{base}/filters/{entry_id}", headers=auth(write_key))
    trigger_id = (
        await client.post(
            f"{base}/triggers", json={"type": "raid", "expr": "echo hi"}, headers=auth(write_key)
        )
    ).json()["id"]
    await client.patch(f"{base}/triggers/{trigger_id}", json={"enabled": False}, headers=auth(write_key))
    await client.delete(f"{base}/triggers/{trigger_id}", headers=auth(write_key))

    entries = (await client.get("/api/v1/audit", headers=auth(write_key))).json()["entries"]
    written = {e["action"]: e["via"] for e in entries if e["action"].split(".")[0] in ("filter", "trigger")}
    assert written == {
        "filter.add": "api",
        "filter.disable": "api",
        "filter.remove": "api",
        "trigger.add": "api",
        "trigger.disable": "api",
        "trigger.remove": "api",
    }


async def test_a_trigger_expression_is_checked_like_one_typed_in_chat(
    client: httpx.AsyncClient, write_key: str
) -> None:
    base = f"/api/v1/channels/{CHANNEL_LOGIN}"
    await client.patch(base, json={"prefix": "?"}, headers=auth(write_key))
    await client.post(f"{base}/filters", json={"pattern": "badword"}, headers=auth(write_key))

    def add(expr: str, type_: str = "raid", **extra: Any) -> Any:
        return client.post(
            f"{base}/triggers", json={"type": type_, "expr": expr, **extra}, headers=auth(write_key)
        )

    unparsable = await add("echo {")
    assert unparsable.status_code == 400 and "placeholder" in unparsable.json()["detail"]
    filtered = await add("echo you badword")
    assert filtered.status_code == 400 and "filter rejects" in filtered.json()["detail"]
    # Parsed under this channel's command sign, not the default one.
    assert (await add("!echo hi")).status_code == 400
    assert (await add("?echo hi")).status_code == 201
    listener = await add("echo {", "listener", match={"regex": "hello"})
    assert listener.status_code == 400
    listed = (await client.get(f"{base}/triggers", headers=auth(write_key))).json()["triggers"]
    assert [t["expr"] for t in listed] == ["?echo hi"]


# ── custom commands, logs and the audit trail ──────────────────────────────
async def test_publications_and_custom_commands_are_public(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], write_key: str
) -> None:
    service: CustomCommandService = app_and_keys[0].state.customcmds_service
    command = await service.create(
        owner_user_id="400",
        owner_login="alice",
        name="hype",
        body="echo hyped",
        channel_id=CHANNEL_ID,
        prefix="!",
    )
    await service.publish(channel_id=CHANNEL_ID, name="hype", command=command, published_by="300")

    public = await client.get(f"/api/v1/channels/{CHANNEL_LOGIN}/publications")
    assert public.status_code == 200
    published = public.json()["publications"]
    assert published[0]["published_as"] == "hype" and published[0]["owner"] == "alice"

    disabled = await client.patch(
        f"/api/v1/channels/{CHANNEL_LOGIN}/publications/hype",
        json={"enabled": False},
        headers=auth(write_key),
    )
    assert disabled.status_code == 200
    again = (await client.get(f"/api/v1/channels/{CHANNEL_LOGIN}/publications")).json()
    assert again["publications"][0]["status"] == "disabled"

    assert (await client.get("/api/v1/custom-commands")).json()["commands"] == []  # nothing global yet


async def test_runs_messages_and_audit_are_readable(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], write_key: str
) -> None:
    chatlog = app_and_keys[0].state.chatlog
    await chatlog.execute(
        "INSERT INTO messages (message_id, channel_id, user_id, user_login, text, sent_at, received_at)"
        " VALUES ('m1', %s, '400', 'alice', 'hello world', %s, %s)",
        (CHANNEL_ID, now_ms(), now_ms()),
    )
    await chatlog.execute(
        "INSERT INTO command_runs (channel_id, user_id, trigger_type, expr, code, duration_ms, at)"
        " VALUES (%s, '400', 'chat', '!ping', 0, 3, %s)",
        (CHANNEL_ID, now_ms()),
    )

    found = await client.get(
        f"/api/v1/channels/{CHANNEL_LOGIN}/messages", params={"q": "world"}, headers=auth(write_key)
    )
    assert [m["text"] for m in found.json()["messages"]] == ["hello world"]
    # FTS5 rejected a lone quote as a syntax error; websearch_to_tsquery is built to take whatever a
    # person types, so an odd query now searches for nothing rather than 400ing at them (ADR-0014).
    odd_query = await client.get(
        f"/api/v1/channels/{CHANNEL_LOGIN}/messages", params={"q": '"'}, headers=auth(write_key)
    )
    assert odd_query.status_code == 200 and odd_query.json()["messages"] == []

    runs = await client.get(f"/api/v1/channels/{CHANNEL_LOGIN}/runs", headers=auth(write_key))
    assert [r["expr"] for r in runs.json()["runs"]] == ["!ping"]

    await client.patch(
        f"/api/v1/channels/{CHANNEL_LOGIN}", json={"quiet_errors": True}, headers=auth(write_key)
    )
    entries = (await client.get("/api/v1/audit", headers=auth(write_key))).json()["entries"]
    assert entries[0]["action"] == "channel.set.quiet_errors" and entries[0]["via"] == "api"


async def test_channel_variables_are_readable(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], write_key: str
) -> None:
    await app_and_keys[0].state.bot_db.execute(
        "INSERT INTO variables (ns, key1, key2, key3, name, value, updated_at, updated_by)"
        " VALUES ('channel', %s, '', '', 'deaths', '7', %s, '300')",
        (CHANNEL_ID, now_ms()),
    )

    body = (await client.get(f"/api/v1/channels/{CHANNEL_LOGIN}/variables", headers=auth(write_key))).json()
    assert body["variables"] == [
        {"name": "deaths", "value": 7, "updated_at": body["variables"][0]["updated_at"], "updated_by": "300"}
    ]


# ── packs as modules, explain, and the ignore list ─────────────────────────
async def _publish_pack_and_command(app: Any) -> None:
    """A global `games` pack holding `coin`, and `hype` published here on its own (under `custom`)."""
    service: CustomCommandService = app.state.customcmds
    packs: PackService = app.state.packs
    made = {}
    for name, body in (("coin", "echo heads"), ("hype", "echo hyped")):
        made[name] = await service.create(
            owner_user_id="400", owner_login="alice", name=name, body=body, channel_id=CHANNEL_ID, prefix="!"
        )
    await service.publish(channel_id=CHANNEL_ID, name="hype", command=made["hype"], published_by="300")
    games = await packs.create(owner_user_id="400", name="games")
    await packs.add_member(games, made["coin"])
    await packs.publish(channel_id=GLOBAL, pack=games, published_by="999")


async def test_the_module_list_has_packs_and_custom_with_chats_toggle_rules(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], write_key: str
) -> None:
    await _publish_pack_and_command(app_and_keys[0])

    async def modules() -> dict[str, dict[str, Any]]:
        found = await client.get(f"/api/v1/channels/{CHANNEL_LOGIN}/modules", headers=auth(write_key))
        return {m["module"]: m for m in found.json()["modules"]}

    listed = await modules()
    assert listed["games"] == {"module": "games", "enabled": True, "toggleable": True, "kind": "pack"}
    assert listed["custom"] == {"module": "custom", "enabled": True, "toggleable": True, "kind": "custom"}
    assert listed["basic"]["kind"] == "builtin"

    policy = app_and_keys[0].state.policy
    actor = Actor(None, "test")
    await policy.mutate(lambda repo: repo.set_module_toggle(GLOBAL, "games", False, actor))
    assert (await modules())["games"]["enabled"] is False  # off everywhere
    put = await client.put(
        f"/api/v1/channels/{CHANNEL_LOGIN}/modules/games", json={"enabled": True}, headers=auth(write_key)
    )
    assert put.status_code == 200
    assert (await modules())["games"]["enabled"] is False  # a global off wins over this channel, as in chat
    await policy.mutate(lambda repo: repo.set_module_toggle(GLOBAL, "games", None, actor))
    assert (await modules())["games"]["enabled"] is True  # now the channel's own toggle counts
    await client.put(
        f"/api/v1/channels/{CHANNEL_LOGIN}/modules/custom", json={"enabled": False}, headers=auth(write_key)
    )
    assert (await modules())["custom"]["enabled"] is False


async def test_explain_knows_pack_commands_and_says_when_their_module_is_off(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], write_key: str
) -> None:
    await _publish_pack_and_command(app_and_keys[0])
    await client.patch(f"/api/v1/channels/{CHANNEL_LOGIN}", json={"prefix": "!"}, headers=auth(write_key))

    async def explain(text: str) -> dict[str, Any]:
        body = {"text": text, "context": "line", "channel": CHANNEL_LOGIN}
        return (await client.post("/api/v1/explain", json=body)).json()  # type: ignore[no-any-return]

    on = await explain("!coin")
    assert on["failure"] is None
    step = on["invocations"][0]
    assert (step["source"], step["module"], step["owner"], step["allowed"]) == (
        "publication", "games", "alice", True,
    )  # fmt: skip

    await client.put(
        f"/api/v1/channels/{CHANNEL_LOGIN}/modules/games", json={"enabled": False}, headers=auth(write_key)
    )
    off = await explain("!coin")
    assert off["invocations"][0]["reason"] == "module off"
    assert off["failure"]["message"] == "coin: module off"  # not "unknown command"

    await client.put(
        f"/api/v1/channels/{CHANNEL_LOGIN}/modules/custom", json={"enabled": False}, headers=auth(write_key)
    )
    assert (await explain("!hype"))["invocations"][0]["reason"] == "module off"
    assert (await explain("!nothing"))["invocations"][0]["reason"] == "unknown command"


async def test_ignored_users_say_who_ignored_them_and_can_be_changed(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], write_key: str
) -> None:
    policy = app_and_keys[0].state.policy
    await policy.mutate(  # `ignore add` by a moderator, and `ignore me` by alice
        lambda repo: repo.set_ignored(CHANNEL_ID, "200", "friend", True, Actor("300", "chat"), reason="spam")
    )
    await policy.mutate(lambda repo: repo.set_ignored(CHANNEL_ID, "400", "alice", True, Actor("400", "chat")))
    url = f"/api/v1/channels/{CHANNEL_LOGIN}/ignored"

    listed = (await client.get(url, headers=auth(write_key))).json()
    assert listed["ignored_everywhere"] == []
    alice, friend = listed["ignored"]
    assert (alice["user_id"], alice["added_by"], alice["added_by_login"]) == ("400", "400", "alice")
    assert {k: friend[k] for k in ("login", "reason", "added_by", "added_by_login")} == {
        "login": "friend", "reason": "spam", "added_by": "300", "added_by_login": "mod",
    }  # fmt: skip
    assert isinstance(friend["added_at"], int) and friend["added_at"] > 1_600_000_000_000  # epoch ms

    body = {"login": "doomtp", "everywhere": True, "reason": "testing"}
    assert (await client.post(url, json=body, headers=auth(write_key))).status_code == 201
    everywhere = (await client.get(url, headers=auth(write_key))).json()["ignored_everywhere"]
    assert [(e["user_id"], e["reason"], e["added_by"]) for e in everywhere] == [(CHANNEL_ID, "testing", None)]
    assert (await client.post(url, json={"login": "nobody"}, headers=auth(write_key))).status_code == 404

    assert (await client.delete(f"{url}/200", headers=auth(write_key))).status_code == 200
    assert (await client.delete(f"{url}/200", headers=auth(write_key))).status_code == 404
    assert (await client.delete(f"{url}/{CHANNEL_ID}", headers=auth(write_key))).status_code == 404
    lifted = await client.delete(f"{url}/{CHANNEL_ID}", params={"everywhere": True}, headers=auth(write_key))
    assert lifted.status_code == 200
    remaining = (await client.get(url, headers=auth(write_key))).json()["ignored"]
    assert [e["user_id"] for e in remaining] == ["400"]

    _, reader = await app_and_keys[1].create(name="reader")
    assert (await client.delete(f"{url}/400", headers=auth(reader))).status_code == 403

    audit = (await client.get("/api/v1/audit", headers=auth(write_key))).json()["entries"]
    by_api = [
        (e["action"], e["target"]) for e in audit if e["via"] == "api" and e["action"].startswith("ignore.")
    ]
    assert by_api == [("ignore.remove", CHANNEL_ID), ("ignore.remove", "200"), ("ignore.add", CHANNEL_ID)]
