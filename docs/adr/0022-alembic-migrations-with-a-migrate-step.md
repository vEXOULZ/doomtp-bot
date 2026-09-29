# ADR-0022: Alembic migrations, run by a migrate step before the bot

**Status:** Accepted — 2026-09-28
**Date:** 2026-09-28
**Deciders:** Project owner

## Context

Migrations are numbered SQL files that the bot applies to itself at startup (ADR-0014). They only go
forward. ADR-0013 listed the risk: an image from before a migration refuses a schema it doesn't know,
so the only way back is a restore from backup.

A deploy then failed twice:

1. The new bot needs its `core` pack (ADR-0019), and nothing had installed it. `deploy/update.sh` runs the
   starter pack first, but a deploy by hand (`docker compose up -d`) does not, and the bot refused to
   start.
2. By then the new bot had already migrated the schema at startup. The rollback to the previous image
   met a schema version it didn't know, and that bot refused to start too.

Two separate causes. What a deploy needs before the bot starts depends on who runs it. And a migration
can't be undone, so a rollback needs a restore.

## Decision

- **Alembic runs the migrations.** Each schema, `bot` and `chatlog`, is its own Alembic environment
  with its own `alembic_version` table, so ADR-0014's split holds. Revisions are plain SQL (`op.execute`),
  with no ORM models. SQLAlchemy is only Alembic's connection layer, over the psycopg driver the bot
  already uses.
- **Every revision has a downgrade.** CI runs each schema down to nothing and back up again, so a
  downgrade that doesn't work fails the build.
- **The existing files become revisions `0001`–`0005` (`bot`) and `0001` (`chatlog`)**, with the SQL
  unchanged. A database that has `schema_migrations` and no `alembic_version` is stamped at its number
  the first time it is upgraded. Those legacy revisions also keep `schema_migrations` up to date in both
  directions, so an image from before this change still reads the version it expects after a
  downgrade.
- **The bot no longer migrates itself.** At startup it checks each schema against its own head revision
  and refuses to start if either is behind (run the migrate step) or ahead (downgrade with the newer
  image first). Tests and `scripts/dev_api.py` still ask `Databases.open` to migrate.
- **A `migrate` one-shot runs before the bot**, from the bot's own image: `doomtp-bot db upgrade`, then
  the starter pack. In compose, the bot `depends_on` it with `service_completed_successfully`, so
  `docker compose up -d` and `deploy/update.sh` both run it. There is no way to start the bot without
  it.
- **`deploy/rollback.sh <image>` rolls back.** It asks the target image for its heads (`doomtp-bot db
  heads`; an image from before this change is asked for its migration count instead). Then it takes a
  backup, downgrades with the *current* image, which is the only one that has the downgrade code, points
  `BOT_IMAGE` at the target and starts it without the migrate step.
- **`doomtp-bot db`** is the command line: `upgrade`, `downgrade --bot REV --chatlog REV`, `current`,
  `heads`. It doesn't take the instance lock, so it can run while the bot is up.

## Options Considered

### Option A: Alembic, a migrate step, downgrades (chosen)

**Pros:** a rollback is a command, not a restore. The standard tool, with `stamp`, `history` and
`current` included. The migrate step makes every deploy do the same thing.
**Cons:** two new dependencies (Alembic, SQLAlchemy). Every migration now needs a downgrade written and
tested. A downgrade that drops a column loses its data, so the rollback script takes a backup first.

### Option B: keep the SQL runner, add `NNNN_name.down.sql` files

**Pros:** no new dependencies.
**Cons:** we would be rebuilding Alembic's revision graph, stamping and history by hand, with our own
bugs. The dependency is small and well known.

### Option C: expand/contract only, no downgrades

Old code tolerates a newer schema if every migration is additive, and the old bot could start as long as
the schema is a known number of steps ahead.
**Pros:** a rollback needs no schema change.
**Cons:** it relies on discipline in every migration, and a single non-additive one breaks it. We still
keep it as a guideline: the old bot keeps running while the migrate step runs, so additive migrations
are safest anyway.

## Consequences

- **Easier:** rolling back an image, even across migrations. Deploying by hand. Seeing where a
  database stands (`doomtp-bot db current`).
- **Harder:** a migration is a Python file with an `upgrade` and a `downgrade`, and both have to work.
  Starting the bot outside compose now needs `doomtp-bot db upgrade` first, and its refusal says so.
- **Sharp edge:** a downgrade that drops a table or column drops the data in it. `rollback.sh` takes a
  backup first, but a rollback followed by a roll-forward won't bring that data back.
- **Sharp edge:** the old bot keeps running while the migrate step runs, so for a moment it sees the new
  schema. Keep migrations additive where possible (Option C's rule), and drop things one release after
  the code stops using them.
- ADR-0013's and ADR-0014's "forward-only migrations" sharp edge is replaced by this ADR.

## Action Items

1. [x] Alembic environments for `bot` and `chatlog`, the existing SQL as revisions with downgrades,
   adoption of `schema_migrations` databases, and a CI test of every downgrade.
2. [x] The startup check, `doomtp-bot db`, the `migrate` one-shot in compose, `update.sh` using it, and
   `deploy/rollback.sh`.
3. [ ] The first real rollback on the server, and its notes back into this ADR.
