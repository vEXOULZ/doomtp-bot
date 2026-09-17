# ADR-0003: Storage — SQLite (WAL) on a volume

**Status:** Accepted (implemented; see Action Items) — 2026-09-17
**Date:** 2026-09-16
**Deciders:** Project owner

## Context

The bot stores two kinds of data:

- **State and config:** OAuth tokens, roles, toggles, cooldown rules, custom commands, the audit log and counters. This is small, but losing it is critical.
- **A log of every chat message** (requirement F1): append-heavy, growing without bound (about 1–5 M rows/year for 1–5 channels), queryable, and including full-text search.

Peak write load is around 50 messages/s, batched. There is exactly one writer process (ADR-0004). It runs in a container on a homelab, where every extra service adds upkeep.

*Revision 2:* the chat log requirement was added. It is the main pressure against SQLite, so it is evaluated explicitly below.

## Decision

- Use **SQLite** in WAL mode, split into **two files** on a mounted volume:
  - `/data/bot.db` holds state and config.
  - `/data/chatlog.db` holds messages, users, mod events and log sessions, with an **FTS5** full-text index.
- The chat log is written in batches (every 500 ms or 200 rows) by a single writer task. External tools read it through a read-only connection (Datasette, the sqlite3 shell).
- Access it through `aiosqlite` with hand-written repositories and numbered SQL migration files.
- Don't use an ORM.

## Options Considered

### Option A: SQLite (chosen)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Very low. It's a single file with nothing to run. |
| Cost | Zero |
| Scalability | Far more than this workload needs. Single writer. |
| Team familiarity | High |

**Pros:** No extra container. Backups are a single file (`.backup`). Tests can run against a temp file with the same engine as production.
**Cons:** One writer at a time. Other services can't share it over the network. Copying the file while it is live is unsafe.

### Option B: PostgreSQL container
| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium. Another service to run, upgrade and back up. |
| Cost | About 50–100 MB RAM, plus upkeep |
| Scalability | High, with concurrent writers |
| Team familiarity | Medium |

**Pros:** Several processes can share it. Richer types and tooling. Ready for a dashboard or workers.
**Cons:** Overkill today. The bot now depends on a second container being healthy at startup.

### Option C: JSON/YAML files
| Dimension | Assessment |
|-----------|------------|
| Complexity | Lowest at first, then grows |
| Cost | Zero |
| Scalability | Poor. Writes aren't atomic and queries aren't possible. |
| Team familiarity | High |

**Pros:** Human-editable.
**Cons:** Races, corruption on crash, and ad-hoc schema drift.

### Chat log specifically: one file, two files, or Postgres?

| Option | Verdict |
|--------|---------|
| Log in the same file as state | Rejected. Unbounded growth mixes with critical state. Backups and retention can't be separated. Long read queries share one WAL with config writes. |
| **Separate `chatlog.db`** (chosen) | Growth, backups and retention are independent. The bot still starts if the log is damaged. FTS5 is built in. SQLite handles single-digit GB and batched inserts of hundreds of rows per second without trouble. |
| Postgres/TimescaleDB for the log only | Better for multi-GB analytics, Grafana and several consumers. Not justified at 1–5 channels. This is the planned migration target. |

## Trade-off Analysis

SQLite gives transactional safety that plain files lack, without the operational cost of Postgres. The single-writer limit matches the single-instance design. The schema carries `channel_id` everywhere and avoids SQLite-only SQL, so moving to Postgres later is a data copy, not a redesign.

## Consequences

- **Easier:** deploying, backing up and testing.
- **Harder:** anything that needs a second process writing, such as a separate dashboard service or background workers.
- **Revisit:** when a second writer shows up (web dashboard as its own service, job workers), migrate to Postgres. Also move `chatlog.db` to Postgres/TimescaleDB if it passes about 10 GB, if FTS queries get slow, or if other services need to query it live.

## Action Items

1. [x] On connect, set `PRAGMA journal_mode=WAL`, `busy_timeout=5000` and `foreign_keys=ON`. *(also `synchronous=NORMAL`)*
2. [x] Write the migrations runner, plus `0001_init.sql` matching the data model in architecture.md.
3. [x] Set file permissions to 600, because the file stores refresh tokens. *(POSIX only; Windows dev hosts keep the default ACL)*
4. [ ] Add a nightly `.backup` job (host cron or sidecar) with rotation.
5. [x] Serialize write transactions per connection (`storage.db.transaction`), because one connection is shared by every writer in the process.
