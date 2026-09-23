# ADR-0015: Metrics — Prometheus text on `/metrics`, written by hand

**Status:** Accepted (implemented; see Action Items) — 2026-09-23
**Date:** 2026-09-23
**Deciders:** Project owner

## Context

[Architecture §13](../architecture.md#13-deployment-and-operations) has listed nine counters since the
first revision — messages logged, backfill inserts, runs by exit code, cancellations, cooldown
rejections, filter hits, outbox drops and EventSub reconnects — and nothing exported any of them. The
outbox kept a `dropped` dictionary that nobody read, and `/readyz` reports a queue depth at one instant.
That leaves a week in a real channel with nothing to read afterwards except logs, which answer "what
happened to this message" well and "how often does this happen" badly (roadmap `ARCH-1`).

The constraints are the usual ones for this project: one process (ADR-0004), no inbound connections from
outside the homelab, an API bound to the LAN (architecture §1, §11), and a preference for no new
dependency unless it pays for itself.

## Decision

- **Format:** the [Prometheus text exposition format](https://prometheus.io/docs/instrumenting/exposition_formats/)
  (version 0.0.4), served as `text/plain; version=0.0.4`. Every scraper speaks it — Prometheus,
  VictoriaMetrics, Grafana Agent, Telegraf, Netdata — and it is also readable with `curl` by a person.
- **Where:** `GET /metrics` on the bot's existing FastAPI app, beside `/healthz` and `/readyz`, with the
  same exposure as those two: no authentication, on the LAN-bound port. A scraper on the LAN pulls; nothing
  is pushed, so the bot still opens no connection it didn't already open.
- **No dependency.** `core/metrics.py` is a counter with labels and a renderer, about sixty lines. Only
  counters are needed; `prometheus_client` would bring gauges, histograms, a registry of process
  collectors and a multiprocess mode this project will never use.
- **Counters live in memory** and start from zero at every start. Prometheus reads a drop as a counter
  reset, which is what `rate()` and `increase()` expect; persisting them would only make a restart
  invisible.
- **Labels never name a channel or a user.** Labels are bounded sets — a message source, an exit code, a
  drop reason, a filter action, a role tier — so the output cannot grow with the audience, and it says
  nothing about who is chatting where. Per-channel detail stays in the database (`command_runs`,
  `outbound_msgs`, `backfill_runs`), where it already is.
- **Each counter is incremented where the thing happens**, not reconstructed from the database: the chat
  log writer counts rows Postgres actually inserted (a backfilled duplicate is not a message logged), the
  runtime counts finished runs, the executor counts commands it reached while their cooldown ran, the
  filter counts hits on what the bot sends, the outbox counts drops, and the Twitch adapter counts
  EventSub welcomes and client restarts.

### EventSub: welcomes and restarts, not "reconnects"

The architecture asked for `eventsub_reconnects_total`. TwitchIO handles reconnects inside the client —
both the ones Twitch asks for (`session_reconnect`) and the ones after a dropped socket — and tells the
application about neither. What it does dispatch is `event_websocket_welcome`, once for every EventSub
session that starts, and the welcome of a reconnect looks exactly like the welcome of a first connection
(checked against the Twitch CLI's mock server). Telling them apart would mean reading TwitchIO's private
`_websockets` state or matching its log messages, both of which a minor upgrade can change silently.

So the bot exports what it can observe honestly:

- `eventsub_welcomes_total` — every EventSub session welcomed. It rises by one per token at startup (the
  bot's, plus one per broadcaster with a full-tier grant). **Any rise after that is a reconnect.** A graph
  of `increase(eventsub_welcomes_total[1h])` shows reconnect churn directly.
- `twitch_client_restarts_total` — the Twitch client stopped on its own and the bot started it again
  (ADR-0001): the reconnect TwitchIO could not handle. Each one also ends and restarts log sessions, so it
  is the one that costs coverage.

## Options Considered

### Option A: a JSON `metrics` section on `/readyz`
**Pros:** no new route; one request shows health and counts together.
**Cons:** nothing scrapes it without a custom exporter, which is the thing this ADR is trying not to need.
It also mixes a probe that must be cheap and decisive (compose and the deploy script read `/readyz`) with a
report that only grows. Rejected.

### Option B: `prometheus_client`
**Pros:** the reference implementation; correct escaping and content negotiation for free, and histograms
if latency is ever measured.
**Cons:** a dependency for four hundred bytes of output. The exposition format for counters is a stable,
line-based text format; getting the escaping right is a few lines and a test. Revisit if histograms are
wanted (see Consequences).

### Option C: push to something (StatsD, OTLP)
**Pros:** no scrape configuration.
**Cons:** the bot would open a new outbound connection to a collector that has to exist first, and
metrics would be lost whenever it didn't. Pull fits a homelab where the bot is the thing being watched.

### Option D: Prometheus text on `/metrics` (chosen)
**Pros:** the lingua franca, readable by hand, no dependency, no new port, no new connection.
**Cons:** unauthenticated. That is the same exposure `/readyz` already has, and the content is less
sensitive than `/readyz`'s (no channel names at all).

## Consequences

- **Easier:** a week in a channel leaves numbers behind. Rates of refused sends, moderation
  cancellations, filter hits and reconnects are one query away instead of a log grep.
- **Harder:** every new counter is a line in `core/metrics.py` and an increment at its source, and label
  sets must stay bounded — a label holding a channel or user ID would be a review-blocking mistake.
- **Sharp edge:** counters reset on every restart and are not persisted. Use `increase()`/`rate()`, never
  a raw value, when a deploy may have happened in the window.
- **Revisit:** if the API ever leaves the LAN behind a reverse proxy (architecture §1), put `/metrics`
  behind the proxy's auth or an API key with a `metrics` scope. If latency histograms are wanted, adopt
  `prometheus_client` rather than growing the hand-written renderer.

## Action Items

1. [x] `core/metrics.py`: labelled counters, the ten below, and the text renderer.
   *(2026-09-23)*
2. [x] `GET /metrics` beside `/healthz` and `/readyz`. *(2026-09-23)*
3. [x] Increment each counter at its source: chat log writer, backfill, runtime, executor, outbox
   filter, outbox drops, Twitch adapter. *(2026-09-23)*
4. [x] Replace `eventsub_reconnects_total` in architecture §13 with `eventsub_welcomes_total` and
   `twitch_client_restarts_total`, and say why. *(2026-09-23)*

| Counter | Labels | Incremented by |
|---------|--------|----------------|
| `messages_logged_total` | `source` (`eventsub`, `recent-messages`) | `ChatLogWriter`, per row Postgres inserted |
| `backfill_inserted_total` | — | `BackfillService.fill` |
| `backfill_incomplete_total` | — | `BackfillService.fill`, per run marked incomplete |
| `runs_total` | `code` | `Runtime.run`, per finished run (not `!explain`'s dry runs) |
| `runs_cancelled_total` | `reason` (`moderated`, `timeout`) | `Runtime.run`, when a run's writes are discarded |
| `cooldown_rejections_total` | `tier` | the executor, when a command it reached is on cooldown (code 128) |
| `filter_hits_total` | `action` (`mask`, `replace`, `tag`, `block`) | `FilterService.apply`, on bot output |
| `outbox_dropped_total` | `reason` | `Outbox` |
| `eventsub_welcomes_total` | — | the Twitch adapter's `event_websocket_welcome` |
| `twitch_client_restarts_total` | — | `TwitchService`, when the client stops on its own |
