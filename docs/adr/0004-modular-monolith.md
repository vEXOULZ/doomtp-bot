# ADR-0004: Process shape — single async modular monolith, one container

**Status:** Proposed
**Date:** 2026-09-16
**Deciders:** Project owner

## Context

The bot's features are chat commands, timers, alerts, counters, quotes and moderation helpers. Each is small, and all of them share the same Twitch connection, rate limit and database. The workload is I/O-bound and tiny. The homelab setup favors few containers and a simple `docker compose up`. A chat bot must also never run twice, or it replies twice.

## Decision

- Run **one asyncio process** in **one container**.
- Internally, split features into **modules** loaded from config. Modules talk through an in-process EventBus and a shared rate-limited Outbox.
- A FastAPI app, run by `uvicorn.Server` inside the same event loop, serves health, readiness, metrics and the OAuth callback. It is the base for a future `/api/v1`.
- The chat logger is a core service in the same process. It writes through its own batching queue, so logging never blocks command handling.
- Enforce a single instance with an exclusive lock file on `/data`.

## Options Considered

### Option A: Modular monolith (chosen)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Low |
| Cost | One container, under 150 MB RAM |
| Scalability | Vertical only. Plenty for this workload. |
| Team familiarity | High |

**Pros:** One thing to deploy, log and restart. Rate limiting is global because there is only one Outbox. There are no network hops between components. Module boundaries still keep the code organized and testable.
**Cons:** A crash or blocking call in one module affects everything. All features deploy together.

### Option B: Split services (ingest → queue → workers → sender)
| Dimension | Assessment |
|-----------|------------|
| Complexity | High. Needs a broker (Redis or NATS), several containers and distributed rate limiting. |
| Cost | 3–5 containers |
| Scalability | Horizontal |
| Team familiarity | Medium |

**Pros:** Isolates failures. Heavy jobs can scale on their own.
**Cons:** Solves problems we don't have. Global chat rate limiting and ordering become distributed problems. Much more upkeep on the homelab.

### Option C: Plain single script
| Dimension | Assessment |
|-----------|------------|
| Complexity | Lowest at first |
| Cost | One container |
| Scalability | Same as A |
| Team familiarity | High |

**Pros:** Fastest to start.
**Cons:** Turns into a monolithic tangle. Hard to test or to switch features on and off.

## Trade-off Analysis

A gets the organization benefits of B (clear boundaries, features that can be toggled, isolated tests) at the operating cost of C. The main risk in A is a module blocking the event loop, so the rules below cover it: nothing blocking in handlers, per-handler exception isolation, and a timeout on each handler.

## Consequences

- **Easier:** deploying, debugging (one log stream), keeping a correct global rate limit, and adding features as modules.
- **Harder:** isolating a misbehaving feature. CPU-heavy work (TTS, image generation) would starve the loop.
- **Revisit:** when a CPU-heavy or long-running feature arrives, move it to a worker process behind a queue. Keep the chat connection and Outbox in the main process.

## Action Items

1. [ ] Define the `Module` base (`setup(ctx)`, `teardown()`, `subscriptions`) and a config-driven loader.
2. [ ] Wrap every handler in try/except with an `asyncio.timeout` (default 5 s).
3. [ ] Implement the `/data/.lock` single-instance guard.
4. [ ] Use a lint rule or review checklist to catch blocking calls such as `requests` or `time.sleep` in `modules/`.
