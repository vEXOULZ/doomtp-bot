"""The web UI: public docs pages and the admin pages (architecture §11)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport

from doomtp_bot.api.app import create_app
from doomtp_bot.core.health import ComponentHealth, HealthRegistry, Status
from doomtp_bot.customcmds.packs import PackService
from doomtp_bot.customcmds.service import CustomCommandService
from doomtp_bot.filters.service import FilterService
from doomtp_bot.modules import builtin_registry
from doomtp_bot.policy.repository import Actor
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.policy.service import PolicyService
from doomtp_bot.runtime.engine import Runtime
from doomtp_bot.storage.db import Databases
from doomtp_bot.triggers.service import TriggerService
from doomtp_bot.webui.auth import SESSION_COOKIE, AdminAuth
from doomtp_bot.webui.emoji import emojify
from doomtp_bot.webui.pages import GRAMMAR_RULES

CHANNEL_ID, CHANNEL_LOGIN = "100", "doomtp"
PASSWORD = "correct horse battery staple"


@pytest.fixture
async def services(dbs: Databases) -> AsyncIterator[dict[str, object]]:
    policy = PolicyService(dbs.bot)
    await policy.reload()
    await policy.mutate(lambda repo: repo.ensure_channel(CHANNEL_ID, CHANNEL_LOGIN, Actor(None, "system")))
    await policy.mutate(
        lambda repo: repo.set_channel_field(CHANNEL_ID, "status", "joined", Actor(None, "system"))
    )
    customcmds = CustomCommandService(dbs.bot)
    packs = PackService(dbs.bot, customcmds)
    triggers = TriggerService(dbs.bot)
    await triggers.reload()
    filters = FilterService(dbs.bot)
    await filters.reload()
    health = HealthRegistry()
    health.register("databases", _ok_check)
    yield {
        "policy": policy,
        "runtime": Runtime(builtin_registry(), policy=policy, services={"policy": policy}),
        "customcmds": customcmds,
        "packs": packs,
        "triggers": triggers,
        "filters": filters,
        "health": health,
    }


async def _ok_check() -> ComponentHealth:
    return ComponentHealth(Status.OK, {"schema": 5})


def _app(services: dict[str, object], *, password: str | None = None) -> object:
    return create_app(
        services["health"],  # type: ignore[arg-type]
        None,
        runtime=services["runtime"],
        policy=services["policy"],
        services={k: v for k, v in services.items() if k not in ("policy", "runtime")},
        admin_password=password,
    )


@pytest.fixture
async def client(services: dict[str, object]) -> AsyncIterator[httpx.AsyncClient]:
    app = _app(services, password=PASSWORD)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


# ── public pages ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", ["/", "/docs/features", "/docs/commands", "/docs/language"])
async def test_public_pages_render(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "doomtp-bot" in response.text


async def test_the_command_page_is_generated_from_the_specs(client: httpx.AsyncClient) -> None:
    body = (await client.get("/docs/commands")).text
    assert "ping" in body and "help [command]" in body
    assert "needs moderator" in body  # role requirements are shown
    assert "shared /" in body or "shared" in body  # cooldown defaults are shown


async def test_the_command_page_is_one_searchable_row_per_command(
    client: httpx.AsyncClient, services: dict[str, object]
) -> None:
    runtime: Runtime = services["runtime"]  # type: ignore[assignment]
    body = (await client.get("/docs/commands")).text
    assert body.count("<details data-search=") == len(runtime.registry.all())
    assert 'class="cmdsearch"' in body
    # The search box matches on the name, the aliases, the module and the summary, lowercased.
    spec = runtime.registry.get("help").spec
    row = [line for line in body.splitlines() if "data-search" in line and f'"{spec.name}' in line]
    assert row and spec.module in row[0] and spec.summary.lower() in row[0]


async def test_globally_published_commands_join_the_reference(
    client: httpx.AsyncClient, services: dict[str, object]
) -> None:
    customcmds: CustomCommandService = services["customcmds"]  # type: ignore[assignment]
    command = await customcmds.create(
        owner_user_id="400", owner_login="alice", name="dice", body="random 1 6"
    )
    await customcmds.publish(channel_id=GLOBAL, name="dice", command=command, published_by="1")
    body = (await client.get("/docs/commands")).text
    assert "dice" in body and "@alice" in body and "published for every channel" in body


async def test_the_command_sign_is_drawn_the_same_on_every_platform(client: httpx.AsyncClient) -> None:
    """The sign is an emoji, so the page draws it as Twemoji rather than leaving it to the reader's font."""
    body = (await client.get("/docs/commands")).text
    assert body.count('<img class="emoji"') > 1
    assert 'alt="🏜"' in body  # copying the sign out of the page still copies the character
    assert body.count("🏜") == body.count('alt="🏜"')  # no bare emoji left over

    image = await client.get("/static/emoji/1f3dc.svg")
    assert image.status_code == 200 and image.headers["content-type"].startswith("image/svg")


def test_emojifying_still_escapes_everything_else() -> None:
    assert str(emojify("<b>x</b>")) == "&lt;b&gt;x&lt;/b&gt;"  # untouched text is escaped as usual
    marked_up = str(emojify("sign: 🏜<script>"))
    assert "&lt;script&gt;" in marked_up and 'src="/static/emoji/1f3dc.svg"' in marked_up
    assert str(emojify("🏜️")).count("<img") == 1  # the variation selector is absorbed


async def test_the_features_page_documents_every_area(client: httpx.AsyncClient) -> None:
    body = (await client.get("/docs/features")).text
    for heading in (
        "Commands and pipelines",
        "Roles and permissions",
        "Cooldowns",
        "Custom commands",
        "Packs and derived commands",
        "Variables",
        "Triggers, listeners and timers",
        "Word filter",
        "Moderation-aware replies",
        "Chat log and history",
        "Channels and onboarding",
        "Explaining an expression",
        "API and admin",
    ):
        assert heading in body, heading


async def test_the_language_page_lists_operators_and_the_grammar(client: httpx.AsyncClient) -> None:
    body = (await client.get("/docs/language")).text
    assert "&amp;&amp;" in body and "E" not in body[:0]  # operators are rendered
    assert "Exit codes" in body and "126" in body
    assert "Grammar" in body  # the checked copy of spec Appendix D


async def test_the_language_page_draws_the_grammar(client: httpx.AsyncClient) -> None:
    """ADR-0011 item 6: a picture per rule, with the text still there underneath."""
    body = (await client.get("/docs/language")).text
    assert body.count('<figure class="railroad">') == len(GRAMMAR_RULES) > 10
    assert "<details>" in body and "Line        ::= Prefix Gap? Expr" in body

    first = await client.get(f"/static/grammar/{GRAMMAR_RULES[0]['name']}.svg")
    assert first.status_code == 200 and first.headers["content-type"].startswith("image/svg")
    assert "railroad-diagram" in first.text


async def test_the_language_page_carries_the_expression_editor(client: httpx.AsyncClient) -> None:
    """ADR-0011: the editor upgrades a plain textarea, so the page works either way."""
    body = (await client.get("/docs/language")).text
    assert "<dtb-editor" in body and 'context="line"' in body
    assert "<textarea" in body  # what someone without JavaScript gets
    assert '<script src="/static/editor/editor.js"' in body

    bundle = await client.get("/static/editor/editor.js")
    assert bundle.status_code == 200
    assert bundle.headers["content-type"].startswith(("text/javascript", "application/javascript"))
    assert "dtb-editor" in bundle.text  # the built bundle is committed, and is the one being served


async def test_a_channel_page_lists_what_is_published(
    client: httpx.AsyncClient, services: dict[str, object]
) -> None:
    customcmds: CustomCommandService = services["customcmds"]  # type: ignore[assignment]
    command = await customcmds.create(
        owner_user_id="400", owner_login="alice", name="hype", body="echo hyped"
    )
    await customcmds.publish(channel_id=CHANNEL_ID, name="hype", command=command, published_by="300")
    body = (await client.get(f"/channels/{CHANNEL_LOGIN}")).text
    assert "hype" in body and "@alice" in body
    assert body.count("<details data-search=") == 1  # the same compact, searchable table

    assert (await client.get("/channels/nobody")).status_code == 404


# ── admin ──────────────────────────────────────────────────────────────────
async def test_admin_needs_a_password_and_redirects_when_signed_out(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin")
    assert response.status_code == 303 and response.headers["location"] == "/admin/login"


async def test_admin_is_disabled_without_a_configured_password(services: dict[str, object]) -> None:
    app = _app(services, password=None)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        assert (await http.get("/admin")).status_code == 404
        assert (await http.get("/admin/login")).status_code == 404
        assert "Sign in" not in (await http.get("/")).text


async def test_signing_in_and_out(client: httpx.AsyncClient) -> None:
    wrong = await client.post("/admin/login", data={"password": "nope"})
    assert wrong.status_code == 303 and "error" in wrong.headers["location"]
    assert SESSION_COOKIE not in client.cookies

    good = await client.post("/admin/login", data={"password": PASSWORD})
    assert good.status_code == 303 and good.headers["location"] == "/admin"
    assert (await client.get("/admin")).status_code == 200

    await client.post("/admin/logout")
    assert (await client.get("/admin")).status_code == 303


async def test_the_dashboard_shows_health_channels_and_the_audit_trail(client: httpx.AsyncClient) -> None:
    await client.post("/admin/login", data={"password": PASSWORD})
    body = (await client.get("/admin")).text
    assert "databases" in body and "schema=5" in body
    assert CHANNEL_LOGIN in body
    assert "channel.join" in body  # the audit row from creating the channel


async def test_the_channel_page_shows_modules_triggers_and_filters(
    client: httpx.AsyncClient, services: dict[str, object]
) -> None:
    triggers: TriggerService = services["triggers"]  # type: ignore[assignment]
    await triggers.add(
        channel_id=CHANNEL_ID,
        type_="listener",
        expr="echo hi",
        match={"regex": "hello"},
        run_as_rank=80,
        created_by="300",
    )
    filters: FilterService = services["filters"]  # type: ignore[assignment]
    await filters.add(channel_id=CHANNEL_ID, pattern="badword", actor_user_id="300")

    await client.post("/admin/login", data={"password": PASSWORD})
    body = (await client.get(f"/admin/channels/{CHANNEL_LOGIN}")).text
    assert "hello" in body and "echo hi" in body  # the listener
    assert "badword" in body  # the filter entry
    assert "core_admin" in body and "always on" in body  # modules, with the ones that can't be disabled


async def test_toggling_a_module_from_the_admin_page(
    client: httpx.AsyncClient, services: dict[str, object]
) -> None:
    policy: PolicyService = services["policy"]  # type: ignore[assignment]
    await client.post("/admin/login", data={"password": PASSWORD})
    page = (await client.get(f"/admin/channels/{CHANNEL_LOGIN}")).text
    csrf = page.split('name="csrf" value="', 1)[1].split('"', 1)[0]

    response = await client.post(
        f"/admin/channels/{CHANNEL_LOGIN}/module",
        data={"module": "basic", "enabled": "off", "csrf": csrf},
    )
    assert response.status_code == 303
    assert policy.snapshot.module_toggles[(CHANNEL_ID, "basic")] is False

    async with await policy.repo.conn.execute(
        "SELECT via FROM audit_log WHERE action = 'module.toggle'"
    ) as cur:
        assert [r["via"] for r in await cur.fetchall()] == ["web"]  # the change is audited as a web action


async def test_a_write_without_a_valid_csrf_token_is_refused(
    client: httpx.AsyncClient, services: dict[str, object]
) -> None:
    policy: PolicyService = services["policy"]  # type: ignore[assignment]
    await client.post("/admin/login", data={"password": PASSWORD})
    response = await client.post(
        f"/admin/channels/{CHANNEL_LOGIN}/module",
        data={"module": "basic", "enabled": "off", "csrf": "forged"},
    )
    assert response.status_code == 403
    assert (CHANNEL_ID, "basic") not in policy.snapshot.module_toggles


async def test_sessions_expire() -> None:
    now = {"t": 0.0}
    auth = AdminAuth(password=PASSWORD, ttl_s=60, clock=lambda: now["t"])
    session = auth.login()
    assert auth.session(session.token) is not None
    now["t"] = 61
    assert auth.session(session.token) is None


def test_the_password_is_not_stored_in_the_clear() -> None:
    auth = AdminAuth(password=PASSWORD)
    assert auth.check_password(PASSWORD) and not auth.check_password("nope")
    assert PASSWORD.encode() not in bytes(auth._digest)  # noqa: SLF001 - the point of the test
