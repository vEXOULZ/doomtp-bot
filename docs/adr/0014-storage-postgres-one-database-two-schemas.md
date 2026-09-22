# ADR-0014: Storage — one Postgres database, two schemas

**Status:** Accepted (implemented; see Action Items) — 2026-09-22
**Date:** 2026-09-22
**Deciders:** Project owner
**Supersedes:** the decision in [ADR-0003](0003-storage-sqlite.md) (its data model and its split between state and log are kept)

## Context

ADR-0003 chose SQLite and, unusually, wrote down the conditions for undoing itself: *"when a second writer
shows up (web dashboard as its own service, job workers), migrate to Postgres."* Two things have now put
that trigger in view:

- **A second writer is coming.** The web UI is meant to become its own service rather than a set of routes
  inside the bot process (ADR-0004 keeps one process; ADR-0011 does not promise the editor stays in it).
  A second process cannot share a SQLite file over the network, and cannot write to it at all without
  fighting the bot for the single write lock.
- **Data safety.** `scripts/backup.py` used SQLite's online backup API and produced a gzipped copy of a
  file. That works, but the restore story is "put the file back", the file is only consistent because
  SQLite made it so, and nothing about it survives the host going away.

Neither of those is urgent on its own. What decided the timing is that **production has not happened yet**:
every row currently stored is disposable. Migrating now costs a schema rewrite and a port of the SQL.
Migrating after launch costs the same rewrite *plus* a data migration, run against the only copy of data
that matters, with the bot down while it happens. The cheap version of this move has an expiry date, and
this ADR spends it.

Measured coupling before deciding: 24 modules touched the database, all through `storage.db` helpers and
hand-written SQL. There is no ORM to swap and no query builder — the port is SQL dialect work, which is
tedious but bounded and fully covered by the suite.

## Decision

- Run **PostgreSQL 17** as a compose service, on a named volume, reachable only on the compose network.
- **One database (`doomtp`), two schemas:** `bot` holds state and configuration, `chatlog` holds the
  message log. Each keeps its own connection with `search_path` pinned to it, so every query still names
  tables unqualified exactly as it did when these were two files, and nothing joins across the two by
  accident.
- Each schema keeps **its own `schema_migrations` table** and its own numbered migration files. Postgres
  has transactional DDL, so a migration and its version row land together or not at all.
- Access through **psycopg 3** (replacing `aiosqlite`), with the same hand-written repositories and
  numbered SQL files. **Still no ORM** — ADR-0003's reasoning there is untouched.
- **Full-text search** is a `tsvector` generated column with a GIN index, queried with
  `websearch_to_tsquery`. Accent folding goes through `chatlog_unaccent()`, a thin `IMMUTABLE` wrapper
  around `unaccent` (a generated column may only call immutable functions).
- **Backups** are `pg_dump --format=custom --schema=<schema>`, one archive per schema, rotated. Restore is
  `pg_restore`.
- The **test suite runs against a real Postgres**, not a stand-in — ADR-0003 wanted the test engine to be
  the production engine and that property is worth keeping.

The existing migrations were collapsed into a single `0001_init.sql` per schema rather than translated one
by one. There is no deployed database whose history they would have to match, and six files describing a
schema nobody ever ran is worse documentation than one file describing the schema everybody runs.

## Options Considered

### Option A: Stay on SQLite
**Pros:** no work, no second container, backups stay a file copy.
**Cons:** the second writer is blocked on it, and the migration only gets more expensive from here — after
launch it is a data migration with real data at stake rather than a schema rewrite with nothing at stake.

### Option B: Two Postgres databases (`bot`, `chatlog`)
**Pros:** the hardest possible isolation; a cross-database join is not expressible.
**Cons:** two of everything for one workload — two backup targets, two health checks, two sets of
credentials, two things to upgrade. The isolation it buys over schemas is isolation against a mistake
nobody is close to making.

### Option C: One database, two schemas (chosen)
**Pros:** one service, one credential, one health check, one upgrade. `pg_dump --schema` keeps state and
log as separate backup and retention decisions, which is the property ADR-0003 actually wanted from two
files. Connections pinned by `search_path` keep the "unqualified table names" property, so the port does
not touch every query twice. If a report ever does want to join a channel to its messages, it becomes
possible rather than impossible.
**Cons:** the wall is a convention rather than a hard boundary. A connection with the wrong `search_path`
could see both.

### Option D: One database, one schema
**Pros:** simplest of all.
**Cons:** throws away the split ADR-0003 made deliberately, and it is the split that lets the log be
backed up, pruned and lost on a different schedule from the state. Rejected.

### Full-text search: FTS5 → what?
| Option | Verdict |
|--------|---------|
| `tsvector` generated column + GIN (chosen) | Maintained by Postgres itself, so the three FTS5 sync triggers disappear. `websearch_to_tsquery` takes user input without raising. |
| A separate search service (Meili, Elastic) | Another service for a feature two people use. No. |
| `LIKE '%…%'` | Correct and slow, and loses accent folding. No. |

## Trade-off Analysis

This buys the second writer, real types (booleans are booleans, `jsonb` is queryable), network access,
concurrent readers that don't block the writer, and a restore story that is a supported tool rather than a
file copy. It costs the exact con ADR-0003 named: **the bot now depends on a second container being
healthy at startup.** That is handled with `depends_on: condition: service_healthy` and a Postgres
healthcheck, so the bot waits rather than crash-looping — but the dependency is real and it is new.

The other real cost is on the dev box: running the tests now needs a Postgres, where before it needed a
temp file. The compose file carries a throwaway `postgres-test` service on a tmpfs for exactly this, and
CI runs one as a service container.

The per-connection write lock from ADR-0003 stays for now. It is no longer load-bearing against the engine
— Postgres handles concurrent writers — but it is still correct for *this* process, where one connection
is shared by every writer. The second writer will need row locks (`SELECT … FOR UPDATE`) in the few places
that read-then-write; those places are marked in the code.

## Consequences

- **Easier:** a second writer, concurrent readers, querying the log from anything that speaks Postgres,
  real types in the schema, and restoring a single table out of a backup.
- **Harder:** one more service to run, upgrade and watch. Running the suite needs a database. A major
  Postgres upgrade is now an operation this project has to care about.
- **Sharp edge:** ADR-0013's forward-only migrations still apply, and now apply to a database that outlives
  the container. Rolling back to an image from before a migration still means rolling back **within a
  schema version**, or restoring a `pg_restore` archive taken before the deploy.
- **Sharp edge (dev, Windows):** psycopg's async mode refuses to run on the Proactor event loop, which is
  Python's default on Windows. `storage.db.configure_event_loop()` switches to the Selector loop, which in
  exchange cannot spawn asyncio subprocesses. Production is Linux, where none of this applies.
- **Tooling:** Datasette is gone — it could only read SQLite. `pgweb` replaces it in the `tools` profile,
  read-only.
- **Revisit:** when the second writer actually lands, replace the single connection per schema with a pool
  and add row locks to the read-then-write paths. If the log passes a size where a single table hurts,
  partition `messages` by month — a Postgres feature, not another migration.

## Action Items

1. [x] Rewrite both `0001_init.sql` files for Postgres, collapsing the old 0001–0006 (bot) and 0001–0002
   (chatlog). *(2026-09-22)*
2. [x] Replace `aiosqlite` with psycopg 3 in `storage/db.py`: schema-pinned connections, transactional
   migrations, the same `fetch_*`/`execute` helpers. *(2026-09-22)*
3. [x] Port every repository's SQL: `ON CONFLICT` for upserts, `RETURNING` for new ids, `GREATEST` for
   two-argument max, booleans for 0/1, `jsonb` for the variable store's numeric leaderboard. *(2026-09-22)*
4. [x] Replace FTS5 and its triggers with a `tsvector` generated column, a GIN index and
   `chatlog_unaccent()`. *(2026-09-22)*
5. [x] `DATABASE_URL` plus `DATABASE_PASSWORD_FILE` in config, replacing the two `*_DB_PATH` settings.
   *(2026-09-22)*
6. [x] Rewrite `scripts/backup.py` around `pg_dump`, keeping its CLI shape and rotation. Install
   `postgresql-client` in the image so the backup service can run it. *(2026-09-22)*
7. [x] Point the suite at a real Postgres: per-session database, per-test rollback, and a committed-database
   fixture for the scripts that open their own connection. Add the service to CI. *(2026-09-22)*
8. [x] Compose: the `postgres` service with a password secret and healthcheck, `postgres-test` on a tmpfs
   under the `test` profile, and `pgweb` replacing Datasette. *(2026-09-22)*
9. [x] Keep `deploy/update.sh` off Postgres: it restarts the bot only, and names the database as a
   dependency so a stopped one still comes up. *(2026-09-22)*
10. [ ] Run the backup and restore on the real guest. The round-trip is proven locally — the backup
    service dumped both schemas from a password-protected server, `DROP SCHEMA bot CASCADE` then
    `pg_restore` brought the schema, its 25 tables and its seeded rows back — but it has never run against
    the guest's own volume, on its own cron, with the dumps then leaving the machine. Blocked on the same
    missing guest as ADR-0013 items 4 and 5.
