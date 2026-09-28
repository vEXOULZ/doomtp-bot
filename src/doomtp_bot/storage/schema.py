"""Schema migrations with Alembic (ADR-0022).

Each schema, `bot` and `chatlog`, is its own Alembic environment under `migrations/<schema>/`, with its
own `alembic_version` table, so ADR-0014's split holds. Revisions are plain SQL, and every one has a
downgrade: a rollback to an older image downgrades with the newer one first (`deploy/rollback.sh`).

The bot doesn't migrate itself. The `migrate` one-shot runs `doomtp-bot db upgrade` before it starts, and
the bot refuses a schema that isn't at its own head (`storage.db.check_schema`).

Alembic is synchronous: it runs over SQLAlchemy with the same psycopg driver, and async callers run it
in a thread.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from importlib import resources

from alembic import command, op
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.pool import NullPool

SCHEMAS = ("bot", "chatlog")


class SchemaMismatch(RuntimeError):
    """The database's schema isn't the one this build was written for."""


def sqlalchemy_url(dsn: str) -> str:
    """The bot's libpq URL as a SQLAlchemy URL on the psycopg (3) driver."""
    for scheme in ("postgresql://", "postgres://"):
        if dsn.startswith(scheme):
            return "postgresql+psycopg://" + dsn[len(scheme) :]
    return dsn


def config(schema: str, dsn: str | None = None) -> Config:
    """An Alembic config for one schema. The DSN travels as an attribute, never through the ini parser,
    which would read a `%` in a password as interpolation."""
    cfg = Config()
    cfg.set_main_option("script_location", f"doomtp_bot.storage:migrations/{schema}")
    cfg.attributes["schema"] = schema
    cfg.attributes["dsn"] = dsn
    return cfg


@cache
def _scripts(schema: str) -> ScriptDirectory:
    return ScriptDirectory.from_config(config(schema))


def head(schema: str) -> str:
    """The newest revision this build has for `schema`."""
    revision = _scripts(schema).get_current_head()
    if revision is None:
        raise RuntimeError(f"no {schema} migrations in this build")
    return revision


def heads() -> dict[str, str]:
    return {schema: head(schema) for schema in SCHEMAS}


def known(schema: str, revision: str) -> bool:
    """Whether this build has `revision`: an unknown one was written by a newer image."""
    return any(script.revision == revision for script in _scripts(schema).walk_revisions())


@contextmanager
def _connect(dsn: str) -> Iterator[Connection]:
    engine = create_engine(sqlalchemy_url(dsn), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            yield conn
    finally:
        engine.dispose()


def _exists(conn: Connection, schema: str, table: str) -> bool:
    return (
        conn.execute(text("SELECT to_regclass(:name)"), {"name": f'"{schema}".{table}'}).scalar() is not None
    )


def _revision(conn: Connection, schema: str) -> str | None:
    if not _exists(conn, schema, "alembic_version"):
        return None
    value = conn.execute(text(f'SELECT version_num FROM "{schema}".alembic_version')).scalar()
    return None if value is None else str(value)


def current(dsn: str) -> dict[str, str | None]:
    """Each schema's revision; None for one Alembic hasn't touched yet."""
    with _connect(dsn) as conn:
        return {schema: _revision(conn, schema) for schema in SCHEMAS}


def _adopt(dsn: str, schema: str) -> None:
    """Stamp a database the numbered-SQL runner (ADR-0014) moved, at the revision its number matches.

    That's a database from before Alembic, or one an image from before ADR-0022 migrated forward again
    after a rollback: its `schema_migrations` is then ahead of `alembic_version`. Later revisions don't
    touch `schema_migrations`, so it never gets ahead of them.
    """
    with _connect(dsn) as conn:
        if not _exists(conn, schema, "schema_migrations"):
            return
        version = conn.execute(text(f'SELECT max(version) FROM "{schema}".schema_migrations')).scalar()
        revision = _revision(conn, schema)
    if version and (revision is None or int(version) > int(revision)):
        command.stamp(config(schema, dsn), f"{int(version):04d}", purge=True)


def upgrade(dsn: str, target: str = "head") -> dict[str, str | None]:
    """Bring both schemas to `target` (their heads by default). Safe to run again."""
    for schema in SCHEMAS:
        _adopt(dsn, schema)
        command.upgrade(config(schema, dsn), target)
    return current(dsn)


def downgrade(dsn: str, targets: dict[str, str]) -> dict[str, str | None]:
    """Take each named schema down to its target revision (`base` empties it)."""
    for schema, target in targets.items():
        _adopt(dsn, schema)
        command.downgrade(config(schema, dsn), target)
    return current(dsn)


# ── used by env.py and the revisions ────────────────────────────────────────
def run_env() -> None:
    """The body of every schema's env.py: migrate that schema over a connection pinned to it."""
    from alembic import context

    cfg = context.config
    schema, dsn = cfg.attributes["schema"], cfg.attributes["dsn"]
    if context.is_offline_mode() or dsn is None:
        raise RuntimeError("migrations run online only, through doomtp_bot.storage.schema")
    with _connect(dsn) as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        conn.exec_driver_sql(f'SET search_path TO "{schema}", public')
        conn.commit()
        context.configure(connection=conn, version_table_schema=schema)
        with context.begin_transaction():
            context.run_migrations()


def run_sql(schema: str, name: str) -> None:
    """Run one of the schema's SQL files as it is, statements and all, inside the migration's transaction.

    Straight to psycopg: SQLAlchemy's `text()` would read `:word` as a bind parameter, and a query with
    parameters can't hold more than one statement.
    """
    sql = (resources.files("doomtp_bot.storage") / "migrations" / schema / "sql" / name).read_text("utf-8")
    driver = op.get_bind().connection.dbapi_connection
    assert driver is not None
    cur = driver.cursor()
    try:
        cur.execute(sql)
    finally:
        cur.close()


def record_legacy(version: int, name: str) -> None:
    """Keep `schema_migrations` in step with revisions 0001–0005, so an image from before ADR-0022 still
    finds the version it expects after a downgrade."""
    op.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version integer PRIMARY KEY, name text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    op.get_bind().execute(
        text("INSERT INTO schema_migrations (version, name) VALUES (:v, :n) ON CONFLICT DO NOTHING"),
        {"v": version, "n": name},
    )


def forget_legacy(version: int) -> None:
    op.get_bind().execute(text("DELETE FROM schema_migrations WHERE version = :v"), {"v": version})


# ── doomtp-bot db … ─────────────────────────────────────────────────────────
def _show(revisions: dict[str, str | None] | dict[str, str]) -> str:
    return " ".join(f"{schema}={revision or 'none'}" for schema, revision in revisions.items())


def cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="doomtp-bot db", description="Schema migrations (ADR-0022)")
    parser.add_argument(
        "--database-url", help="default: DATABASE_URL and its password, as the bot reads them"
    )
    sub = parser.add_subparsers(dest="action", required=True)
    up = sub.add_parser("upgrade", help="bring both schemas to this build's heads")
    up.add_argument("--to", default="head", help="a revision other than head (both schemas)")
    down = sub.add_parser("downgrade", help="take schemas down to older revisions")
    for schema in SCHEMAS:
        down.add_argument(
            f"--{schema}", metavar="REV", help=f"target revision for {schema} (base empties it)"
        )
    sub.add_parser("current", help="where the database stands")
    sub.add_parser("heads", help="the newest revisions this build has; needs no database")
    args = parser.parse_args(argv)

    if args.action == "heads":
        print(_show(heads()))
        return 0
    if args.database_url:
        dsn = args.database_url
    else:
        from doomtp_bot.config import Settings

        dsn = Settings().database_dsn()
    if args.action == "upgrade":
        print(_show(upgrade(dsn, args.to)))
    elif args.action == "downgrade":
        targets = {schema: getattr(args, schema) for schema in SCHEMAS if getattr(args, schema)}
        if not targets:
            parser.error("name a target: --bot REV and/or --chatlog REV")
        print(_show(downgrade(dsn, targets)))
    else:
        print(_show(current(dsn)))
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
