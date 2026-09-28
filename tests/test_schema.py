"""Alembic migrations (ADR-0022): every revision goes down and back up, and old databases are adopted."""

from __future__ import annotations

from importlib import resources
from typing import Any

import psycopg
import pytest
from alembic import command

from doomtp_bot.storage import schema
from doomtp_bot.storage.db import Databases, check_schema, connect


def _catalog(dsn: str) -> dict[str, Any]:
    """Everything a migration can change in `bot` and `chatlog`, apart from Alembic's own table."""
    with psycopg.connect(dsn) as conn:
        columns = conn.execute(
            "SELECT table_schema, table_name, column_name, data_type, is_nullable, column_default,"
            " generation_expression FROM information_schema.columns"
            " WHERE table_schema IN ('bot', 'chatlog') AND table_name <> 'alembic_version' ORDER BY 1, 2, 3"
        ).fetchall()
        indexes = conn.execute(
            "SELECT schemaname, indexname, indexdef FROM pg_indexes"
            " WHERE schemaname IN ('bot', 'chatlog') AND tablename <> 'alembic_version' ORDER BY 1, 2"
        ).fetchall()
        functions = conn.execute(
            "SELECT n.nspname, p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname IN ('bot', 'chatlog') ORDER BY 1, 2"
        ).fetchall()
    return {"columns": columns, "indexes": indexes, "functions": functions}


def _revisions(name: str) -> list[str]:
    """The schema's revisions, oldest first."""
    return [script.revision for script in reversed(list(schema._scripts(name).walk_revisions()))]


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
    assert len({repr(catalog) for catalog in seen.values()}) == len(levels), "a revision changed nothing"
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
            "SELECT schemaname || '.' || tablename FROM pg_tables WHERE schemaname IN ('bot', 'chatlog')"
            " ORDER BY 1"
        ).fetchall()
    assert tables == [("bot.alembic_version",), ("chatlog.alembic_version",)]
    assert schema.current(empty_database) == {"bot": None, "chatlog": None}


def test_legacy_revisions_keep_schema_migrations_in_step(empty_database: str) -> None:
    """An image from before ADR-0022 reads `schema_migrations`, so a downgrade has to update it."""
    schema.upgrade(empty_database)
    schema.downgrade(empty_database, {"bot": "0002"})
    with psycopg.connect(empty_database) as conn:
        rows = conn.execute("SELECT version, name FROM bot.schema_migrations ORDER BY 1").fetchall()
    assert rows == [(1, "0001_init.sql"), (2, "0002_quotes.sql")]


def test_a_database_from_before_alembic_is_adopted(empty_database: str) -> None:
    """The SQL runner's database: the same tables, `schema_migrations` rows and no `alembic_version`."""
    schema.upgrade(empty_database)
    before = _catalog(empty_database)
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
