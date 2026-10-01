"""Alembic migrations (ADR-0022): every revision goes down and back up, and old databases are adopted."""

from __future__ import annotations

from importlib import resources
from typing import Any

import psycopg
import pytest
from alembic import command

from doomtp_bot.storage import schema
from doomtp_bot.storage.db import Databases, check_schema, connect

# The schemas the revisions write: the bot's two, and vex-platform's `jobs` and `public.audit_log` (ADR-0027).
TOUCHED = ["bot", "chatlog", "jobs", "public"]


def _catalog(dsn: str) -> dict[str, Any]:
    """Everything a migration can change in the schemas it touches, apart from Alembic's own table."""
    with psycopg.connect(dsn) as conn:
        columns = conn.execute(
            "SELECT table_schema, table_name, column_name, data_type, is_nullable, column_default,"
            " generation_expression FROM information_schema.columns"
            " WHERE table_schema = ANY(%s) AND table_name <> 'alembic_version' ORDER BY 1, 2, 3",
            [TOUCHED],
        ).fetchall()
        indexes = conn.execute(
            "SELECT schemaname, indexname, indexdef FROM pg_indexes"
            " WHERE schemaname = ANY(%s) AND tablename <> 'alembic_version' ORDER BY 1, 2",
            [TOUCHED],
        ).fetchall()
        functions = conn.execute(
            "SELECT n.nspname, p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = ANY(%s)"
            # Not an extension's own (chatlog 0001 leaves `unaccent` installed in `public`).
            " AND NOT EXISTS (SELECT FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e') ORDER BY 1, 2",
            [TOUCHED],
        ).fetchall()
    return {"columns": columns, "indexes": indexes, "functions": functions}


def _revisions(name: str) -> list[str]:
    """The schema's revisions, oldest first."""
    return [script.revision for script in reversed(list(schema._scripts(name).walk_revisions()))]


def _data_only(name: str, revision: str) -> bool:
    """A revision that moves rows and leaves the catalog alone says so with `data_only = True`."""
    return bool(getattr(schema._scripts(name).get_revision(revision).module, "data_only", False))


@pytest.mark.parametrize("name", schema.SCHEMAS)
def test_revisions_are_numbered_in_one_line(name: str) -> None:
    """Adoption compares revision ids as numbers (`schema._adopt`), so they stay NNNN and contiguous."""
    revisions = _revisions(name)
    assert revisions == [f"{n:04d}" for n in range(1, len(revisions) + 1)]
    assert schema.head(name) == revisions[-1]


@pytest.mark.parametrize("name", schema.SCHEMAS)
def test_every_revision_goes_down_and_back_up(empty_database: str, name: str) -> None:
    """Step up one revision at a time, then down again: each level looks the same both ways."""
    levels = ["base", *_revisions(name)]
    seen = {"base": _catalog(empty_database)}
    for revision in levels[1:]:
        command.upgrade(schema.config(name, empty_database), revision)
        seen[revision] = _catalog(empty_database)
    for previous, revision in zip(levels, levels[1:], strict=False):
        changed = seen[revision] != seen[previous]
        assert changed != _data_only(name, revision), (
            f"{revision} changed nothing, or data_only changed tables"
        )
    for previous in reversed(levels[:-1]):
        schema.downgrade(empty_database, {name: previous})
        assert _catalog(empty_database) == seen[previous], f"{name} down to {previous}"
    assert schema.upgrade(empty_database) == schema.heads()


def test_upgrade_twice_changes_nothing(empty_database: str) -> None:
    assert schema.upgrade(empty_database) == schema.heads()
    before = _catalog(empty_database)
    assert schema.upgrade(empty_database) == schema.heads()
    assert _catalog(empty_database) == before


def test_base_leaves_nothing_behind(empty_database: str) -> None:
    schema.upgrade(empty_database)
    schema.downgrade(empty_database, {"bot": "base", "chatlog": "base"})
    with psycopg.connect(empty_database) as conn:
        tables = conn.execute(
            "SELECT schemaname || '.' || tablename FROM pg_tables WHERE schemaname = ANY(%s) ORDER BY 1",
            [TOUCHED],
        ).fetchall()
        jobs = conn.execute("SELECT to_regnamespace('jobs')").fetchone()
    assert tables == [("bot.alembic_version",), ("chatlog.alembic_version",)]
    assert jobs == (None,)
    assert schema.current(empty_database) == {"bot": None, "chatlog": None}


def test_the_shared_audit_table_leaves_the_bots_own_in_place(empty_database: str) -> None:
    """0011 puts vex-platform's `audit_log` in `public` (ADR-0027): on the bot's own connection, an
    unqualified `audit_log` is still `bot.audit_log`."""
    schema.upgrade(empty_database)
    with psycopg.connect(empty_database) as conn:
        conn.execute("SET search_path TO bot, public")
        row = conn.execute(
            "SELECT to_regclass('audit_log') = to_regclass('bot.audit_log'),"
            " to_regclass('public.audit_log') IS NOT NULL, to_regclass('jobs.job_runs') IS NOT NULL"
        ).fetchone()
    assert row == (True, True, True)


def test_legacy_revisions_keep_schema_migrations_in_step(empty_database: str) -> None:
    """An image from before ADR-0022 reads `schema_migrations`, so a downgrade has to update it."""
    schema.upgrade(empty_database)
    schema.downgrade(empty_database, {"bot": "0002"})
    with psycopg.connect(empty_database) as conn:
        rows = conn.execute("SELECT version, name FROM bot.schema_migrations ORDER BY 1").fetchall()
    assert rows == [(1, "0001_init.sql"), (2, "0002_quotes.sql")]


def test_a_database_from_before_alembic_is_adopted(empty_database: str) -> None:
    """The SQL runner's database: the tables of its last files (bot 0005, chatlog 0001), `schema_migrations` rows and no
    `alembic_version`. Adopted there, it takes the later revisions like any other database."""
    schema.upgrade(empty_database)
    before = _catalog(empty_database)
    schema.downgrade(empty_database, {"bot": "0005", "chatlog": "0001"})
    with psycopg.connect(empty_database) as conn:
        conn.execute("DROP TABLE bot.alembic_version")
        conn.execute("DROP TABLE chatlog.alembic_version")
    assert schema.current(empty_database) == {"bot": None, "chatlog": None}
    assert schema.upgrade(empty_database) == schema.heads()
    assert _catalog(empty_database) == before


def test_an_old_image_that_migrated_forward_again_is_adopted(empty_database: str) -> None:
    """After a rollback, an image from before ADR-0022 runs its own SQL runner and gets ahead of Alembic."""
    schema.upgrade(empty_database)
    schema.downgrade(empty_database, {"bot": "0003"})
    sql = resources.files("doomtp_bot.storage") / "migrations" / "bot" / "sql"
    with psycopg.connect(empty_database) as conn:  # what that runner does: the file, then its row
        conn.execute("SET search_path TO bot, public")
        for version, name in [
            (4, "0004_more_variable_limits.sql"),
            (5, "0005_internal_and_system_packs.sql"),
        ]:
            conn.execute((sql / name).read_text("utf-8").encode())
            conn.execute("INSERT INTO schema_migrations (version, name) VALUES (%s, %s)", (version, name))
    assert schema.current(empty_database)["bot"] == "0003"
    assert schema.upgrade(empty_database) == schema.heads()


async def test_the_bot_refuses_a_schema_behind_its_own(empty_database: str) -> None:
    schema.upgrade(empty_database)
    schema.downgrade(empty_database, {"bot": "0004"})
    with pytest.raises(schema.SchemaMismatch, match="doomtp-bot db upgrade"):
        await Databases.open(empty_database)
    dbs = await Databases.open(empty_database, migrate=True)
    await dbs.close()


async def test_the_bot_refuses_a_schema_a_newer_build_wrote(empty_database: str) -> None:
    schema.upgrade(empty_database)
    with psycopg.connect(empty_database) as conn:
        conn.execute("UPDATE chatlog.alembic_version SET version_num = '9999'")
    conn = await connect(empty_database, "chatlog")
    try:
        with pytest.raises(schema.SchemaMismatch, match="rollback"):
            await check_schema(conn, "chatlog")
    finally:
        await conn.close()


async def test_the_bot_refuses_an_empty_database(empty_database: str) -> None:
    with pytest.raises(schema.SchemaMismatch, match="at nothing"):
        await Databases.open(empty_database)


def test_heads_needs_no_database(capsys: pytest.CaptureFixture[str]) -> None:
    """`deploy/rollback.sh` asks the target image this, without settings or a database."""
    assert schema.cli(["heads"]) == 0
    assert capsys.readouterr().out.strip() == f"bot={schema.head('bot')} chatlog={schema.head('chatlog')}"


def test_the_command_line_moves_both_ways(empty_database: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert schema.cli(["--database-url", empty_database, "upgrade"]) == 0
    assert schema.cli(["--database-url", empty_database, "downgrade", "--bot", "0003"]) == 0
    assert schema.cli(["--database-url", empty_database, "current"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"bot={schema.head('bot')} chatlog={schema.head('chatlog')}"
    assert out[1] == out[2] == f"bot=0003 chatlog={schema.head('chatlog')}"


def test_the_triggers_module_moves_to_automation_and_back(empty_database: str) -> None:
    """0006 renames the module in its toggles and callbacks (ADR-0019), and keeps a row already renamed."""
    command.upgrade(schema.config("bot", empty_database), "0005")
    with psycopg.connect(empty_database) as conn:
        conn.execute(
            "INSERT INTO bot.module_toggles VALUES ('c1', 'triggers', false), ('c2', 'triggers', false),"
            " ('c2', 'automation', true), ('c1', 'quotes', false)"
        )
        conn.execute(
            "INSERT INTO bot.callbacks (channel_id, scope, kind, expr, syntax_version, updated_at)"
            " VALUES ('c1', 'module:triggers', 'on_denied', 'echo no', '1', 0)"
        )

    def rows() -> list[tuple[str, ...]]:
        with psycopg.connect(empty_database) as conn:
            toggles = conn.execute(
                "SELECT channel_id, module, enabled::text FROM bot.module_toggles ORDER BY 1, 2"
            )
            scopes = conn.execute("SELECT channel_id, scope FROM bot.callbacks ORDER BY 1, 2")
            return [*toggles.fetchall(), *scopes.fetchall()]

    command.upgrade(schema.config("bot", empty_database), "0006")
    assert rows() == [
        ("c1", "automation", "false"),
        ("c1", "quotes", "false"),
        ("c2", "automation", "true"),
        ("c2", "triggers", "false"),  # c2 already had an automation row, which wins
        ("c1", "module:automation"),
    ]
    schema.downgrade(empty_database, {"bot": "0005"})
    assert ("c1", "triggers", "false") in rows() and ("c1", "module:triggers") in rows()


def _quote_rows(dsn: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT channel_id, number, text, game, added_at, deleted_at IS NOT NULL FROM bot.quotes"
            " ORDER BY 1, 2"
        ).fetchall()


def test_quotes_move_into_the_bots_variables_and_back(empty_database: str) -> None:
    """0008 copies the live quotes into `publisher.channel.quotes` under the bot's account, keeps the last
    number given out, and drops the table (ADR-0019 item 9); down, the table comes back from them."""
    day = 1_790_006_400_000  # 2026-09-21 16:00 UTC, already the 22nd in Tokyo
    command.upgrade(schema.config("bot", empty_database), "0007")
    with psycopg.connect(empty_database) as conn:
        conn.execute(
            "INSERT INTO bot.oauth_tokens (identity, user_id, login, access_token, updated_at)"
            " VALUES ('bot', '999', 'thebot', 'x', 0)"
        )
        conn.execute(
            "INSERT INTO bot.channels (channel_id, login, timezone, added_at, updated_at) VALUES ('c1', 'one', 'Asia/Tokyo', 0, 0)"
        )
        conn.execute(
            "INSERT INTO bot.quotes (channel_id, number, text, game, added_by, added_at, deleted_at) VALUES"
            " ('c1', 1, 'first', 'Doom', '5', %(d)s, NULL), ('c1', 2, 'gone', NULL, '5', %(d)s, %(d)s),"
            " ('c1', 3, 'I meant to do that', NULL, '5', %(d)s, NULL), ('c1', 4, 'also gone', NULL, '5', %(d)s, %(d)s),"
            " ('c2', 1, 'elsewhere', NULL, '6', %(d)s, NULL)",
            {"d": day},
        )  # fmt: skip

    command.upgrade(schema.config("bot", empty_database), "0008")
    with psycopg.connect(empty_database) as conn:
        assert conn.execute("SELECT to_regclass('bot.quotes')").fetchone() == (None,)
        stored = conn.execute(
            "SELECT key1, key2, name, value FROM bot.variables WHERE ns = 'publisher.channel' ORDER BY 2, 3"
        ).fetchall()
    assert stored == [
        ("999", "c1", "quote_next", "4"),
        ("999", "c1", "quotes", '{"1":{"text":"first","date":"2026-09-22","game":"Doom"},'
                                '"3":{"text":"I meant to do that","date":"2026-09-22"}}'),
        ("999", "c2", "quote_next", "1"),
        ("999", "c2", "quotes", '{"1":{"text":"elsewhere","date":"2026-09-21"}}'),
    ]  # fmt: skip

    schema.downgrade(empty_database, {"bot": "0007"})
    midnight = 1_790_035_200_000  # 2026-09-22 00:00 UTC
    assert _quote_rows(empty_database) == [
        ("c1", 1, "first", "Doom", midnight, False),
        ("c1", 3, "I meant to do that", None, midnight, False),
        ("c1", 4, "", None, 0, True),  # the last number stays taken
        ("c2", 1, "elsewhere", None, midnight - 86_400_000, False),
    ]
    with psycopg.connect(empty_database) as conn:
        assert conn.execute("SELECT count(*) FROM bot.variables").fetchone() == (0,)


def test_quotes_need_the_bot_account_to_move(empty_database: str) -> None:
    command.upgrade(schema.config("bot", empty_database), "0007")
    with psycopg.connect(empty_database) as conn:
        conn.execute("INSERT INTO bot.quotes (channel_id, number, text, added_at) VALUES ('c1', 1, 'x', 0)")
    with pytest.raises(RuntimeError, match="no bot account"):
        command.upgrade(schema.config("bot", empty_database), "0008")
    assert _quote_rows(empty_database) == [("c1", 1, "x", None, 0, False)]  # nothing moved, nothing lost


def test_a_channel_with_many_quotes_gets_room_for_them(empty_database: str) -> None:
    """One channel's quotes are one value: the bot's cap and quota grow to fit it, with room to spare."""
    command.upgrade(schema.config("bot", empty_database), "0007")
    with psycopg.connect(empty_database) as conn:
        conn.execute(
            "INSERT INTO bot.oauth_tokens (identity, user_id, login, access_token, updated_at)"
            " VALUES ('bot', '999', 'thebot', 'x', 0)"
        )
        conn.execute(
            "INSERT INTO bot.quotes (channel_id, number, text, added_at)"
            " SELECT 'c1', n, repeat('q', 390), 0 FROM generate_series(1, 1000) n"
        )
    command.upgrade(schema.config("bot", empty_database), "0008")
    with psycopg.connect(empty_database) as conn:
        size = conn.execute("SELECT size_bytes FROM bot.variables WHERE name = 'quotes'").fetchone()
        limits = conn.execute(
            "SELECT quota_bytes, value_cap_bytes FROM bot.variable_limits WHERE owner_kind = 'publisher'"
        ).fetchone()
    assert size is not None and limits is not None
    assert size[0] > 262_144 and limits[1] == 2 * size[0]
    assert limits[0] is None  # ~420 KB still fits the 1 MB quota
