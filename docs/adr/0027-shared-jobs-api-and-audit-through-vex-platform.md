# ADR-0027: Shared jobs, API and audit through vex-platform

**Status:** Accepted — 2026-09-30 (amends ADR-0006 §5 and ADR-0024's backfill queue)
**Date:** 2026-09-30
**Deciders:** Project owner

## Context

doomtp-bot and twitch-archive each grew their own background jobs, admin API and audit log. The
archive reads the bot's chat log and the dashboard talks to both, so the drift costs twice: every
feature is built twice, and the two APIs answer in different shapes.

- **Jobs:** `chatlog.backfill_jobs` and `BackfillQueue` run one job at a time. Queued jobs are
  deduplicated through partial unique indexes, but nothing retries, a running job can't be cancelled,
  and nothing reports progress.
- **API:** errors are FastAPI's `{detail}`, times are epoch ms, and every list has its own key
  (`{jobs: [...]}`).
- **Audit:** `bot.audit_log` is written in the same transaction as the change, with dotted actions and
  `before`/`after`. That part is right. But the shape is the bot's own: text values, ms times, no
  outcome, and no link to a request or a job.

[vex-platform](https://github.com/vEXOULZ/vex-platform) is the shared package: a job runtime on
procrastinate, the `/api/v2` conventions and an audit log, with its rules written down in its
`docs/conventions.md`. twitch-archive already runs on it.

## Decision

### The dependency

`vex-platform` is installed from its release tarball, pinned to a tag
(`vex-platform @ https://github.com/vEXOULZ/vex-platform/archive/refs/tags/vX.Y.Z.tar.gz`). The image
has no git, and a tarball needs none. Moving to a newer version is a pull request that changes the URL
and `uv.lock`.

### Where its tables live

Bot revision 0011 creates them, additively. Nothing reads or writes them until later revisions and
code switch over:

- **`jobs`**, a Postgres schema of its own: procrastinate 3.10.0's tables and functions, plus
  `job_runs` and `job_run_events`. The runtime reaches them through its own psycopg pool, with its
  `search_path` set to `jobs`. The bot's shared connections (ADR-0014) never touch them, so a long
  job no longer holds the `chatlog` connection's write lock.
- **`public.audit_log`**, the shared audit table. `bot.audit_log` keeps its name, so the new table
  can't be called `bot.audit_log` too. The bot's connections search `"<schema>", public`, so an
  unqualified `audit_log` still means the old table. New code always writes `public.audit_log`.

The SQL is vex-platform's, frozen per revision (`jobs_sql(1)`, `audit_sql(1)`). A later procrastinate
version arrives as `jobs_sql(2)`, applied by a new bot revision. The downgrade drops both.

### Audit rows move to the shared table

`write_audit()` and `read_audit()` keep their signatures and become a shim over `public.audit_log`,
so none of their callers change. The shim maps the old fields onto the new ones:

| `bot.audit_log` | `public.audit_log` |
|---|---|
| `actor_user_id` | `actor_kind = 'user'`, `actor_id`; `system` when there is none |
| `via` | `via`; `script` becomes `cli`, and a surface the table has no name for becomes `system` with the bot's name in `detail` |
| `channel_id` | `scope` (`NULL` for a global change) |
| `before`, `after` (text) | `before`, `after` (jsonb: the text parsed as JSON, or kept as a JSON string) |
| `at` (ms) | `at` (timestamptz) |
| — | `outcome = 'ok'`, `request_id`, `job_run_id` |

The rows already in `bot.audit_log` are copied at startup, each tagged with
`request_id = 'bot.audit_log:<id>'`, so a copy that runs twice adds nothing. The copy runs at every
start, not in the migration. A rollback to an image from before the switch keeps writing the old
table, and the next start picks those rows up. `bot.audit_log` stays until a later, confirmed cleanup.

The nightly backup (`scripts/backup.py`) dumps only `bot` and `chatlog`. Once rows are written to
`public.audit_log`, it dumps that table as well. It leaves `jobs` out: job runs are working state, not
history, and what a backfill produced is the log it filled.

### Backfill runs as a job kind

`chat_backfill` replaces `BackfillQueue`'s worker (`history/jobs.py`). Each gaps job gets a dedupe key per
channel (`queued_key`), so a second request merges into the queued one as it does today, widening its
range. The same range is never queued or running twice (`active_key`). Every run takes one lock, and the
runtime runs one at a time, so the rate-limited provider still sees a single caller.

- **Cancel** is cooperative: `fill_many` asks `ctx.should_stop()` before each request, and the step
  stops there. Cancelling a task outright could leave a savepoint half-done on a shared connection. A
  cancel from `!backfill cancel` or `DELETE /api/v1/.../backfill/{id}` now stops a running job too, not
  only a queued one; the answer waits up to two seconds for it to stop.
- **Progress** comes from `fill_many`, one item per gap.
- **A provider pause** (its daily budget spent) holds the job in its step until the time the provider
  gave, still answering a cancel; it isn't a failure. A failed fill is retried with the runtime's
  backoff, from where it stopped; a channel the provider doesn't log fails at once.
- **Consent** stays in the step: a job whose channel turned backfill off while it waited cancels itself.
- `!backfill` and the v1 routes queue, list and cancel through the runtime, with the same replies. The
  ids are run ids, and the lists show only runs queued since the switch.
- **Jobs an older image left** queued or running in `chatlog.backfill_jobs` are queued again as runs at
  each start, keyed by their old id (`legacy_id`), so they run once. The old table is only read.

### `/api/v2`

The jobs and audit routers come from vex-platform and are mounted under `/api/v2`, behind the same
access rules as v1 (ADR-0017, ADR-0026). Their errors are problem+json, scoped to v2 so v1 keeps
`{detail}`. v1 stays until its clients (the web editor, the archive's log reader) have moved.

### Metrics

The Prometheus counters on `/metrics` (ADR-0015) stay. The backfill counters are incremented from the
runtime's hooks instead of from the queue.

## Alternatives considered

| Option | Why not |
|---|---|
| Keep `BackfillQueue` and copy the API conventions by hand | Fixes neither the missing retry, cancel and progress, nor the drift. |
| A git dependency (`git+https://...@vX.Y.Z`) | The image has no git, and adding git only to install one package is waste. |
| Rename `bot.audit_log` and put the shared table in its place | Not additive: an image rolled back to an older version would write a table that is no longer there. |
| Copy the audit rows in the migration | Rows the old image writes after the migration, or after a rollback, would be missed. The startup copy catches them. |

## Action items

1. [x] Depend on vex-platform v0.2.0. Bot revision 0011 creates the `jobs` schema and `public.audit_log`.
2. [x] `write_audit()`/`read_audit()` over `public.audit_log`, copy `bot.audit_log` at startup, and add
   `public.audit_log` to the backup.
3. [x] The `chat_backfill` job kind replaces `BackfillQueue`'s worker.
4. [ ] `/api/v2` jobs and audit routes, with problem+json.
5. [ ] The backfill counters come from the runtime's hooks.
6. [ ] With the owner's confirmation, once the clients have moved: remove the v1 job routes and `BackfillQueue`,
   stop writing `bot.audit_log` and `chatlog.backfill_jobs`, then drop them.
