# ADR-0002: Twitch client library — TwitchIO 3.x behind an adapter

**Status:** Accepted (implemented; see Action Items) — 2026-09-17
**Date:** 2026-09-16
**Deciders:** Project owner

## Context

Talking to Twitch requires several pieces:

- The OAuth flow and token refresh
- The EventSub WebSocket lifecycle
- Helix calls with rate-limit handling

Writing all of that from scratch is a large chunk of work that has nothing to do with the bot's features. Python has two maintained async libraries for it. The project has one maintainer, so how quickly the library tracks Twitch API changes matters more than fine control.

## Decision

- Use **TwitchIO 3.x** for EventSub, Helix and token management.
- Confine it to `src/doomtp_bot/twitch/`. The rest of the code sees only our own domain events (`ChatMessage`, `Raid`…) and a small `TwitchApi` protocol (`send_message`, `timeout`, `get_stream`…).
- Don't use TwitchIO's built-in commands extension. Command routing lives in `core/` so it stays testable and independent of the library.

## Options Considered

### Option A: TwitchIO 3.x (chosen)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Low-medium |
| Cost | Free, MIT license |
| Scalability | More than enough |
| Team familiarity | The most widely used Python Twitch bot library |

**Pros:** Designed around EventSub starting with v3. Manages the WebSocket lifecycle and token refresh, with hooks to persist tokens. Asyncio native. Active community.
**Cons:** The 3.x API broke compatibility with 2.x, so many online examples are stale. It has its own opinions (Bot, Components) that we partly bypass.

### Option B: twitchAPI (pyTwitchAPI)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Low-medium |
| Cost | Free, MIT license |
| Scalability | More than enough |
| Team familiarity | Medium |

**Pros:** Very complete, strongly typed Helix coverage. Solid EventSub WebSocket support and a user-auth helper.
**Cons:** Oriented toward the API rather than bots, so there is more glue to write for chat bot ergonomics. Its chat module historically centered on IRC.

### Option C: Hand-rolled (aiohttp + websockets)
| Dimension | Assessment |
|-----------|------------|
| Complexity | High |
| Cost | Maintainer time |
| Scalability | Whatever we build |
| Team familiarity | n/a |

**Pros:** Full control, few dependencies, no library churn.
**Cons:** Twitch protocol details, edge cases and API changes all become our problem to handle and keep up with.

## Trade-off Analysis

A and B are close. A wins on chat-bot fit, since its bot ergonomics come from its IRC-era history and it now runs on EventSub. B would be better if the bot were mostly a Helix automation tool. Because of the adapter boundary, the choice is **cheap to reverse**: switching to B, or to C, touches only `twitch/`. C is rejected because maintaining protocol code is the wrong use of a single maintainer's time.

## Consequences

- **Easier:** getting to a working bot quickly. Testing modules with a `FakeTwitch` adapter.
- **Harder:** a little mapping code between TwitchIO models and domain events. The TwitchIO version must be pinned and upgrades need care.
- **Revisit:** if TwitchIO stalls or lags behind a Twitch API change, swap in twitchAPI behind the same protocol.

## Action Items

1. [x] Pin `twitchio>=3,<4` in `pyproject.toml`, with `uv.lock` pinning the exact resolved set (twitchio 3.3.2 today). The image and CI install with `uv sync --locked`, which fails if the lock is stale.
2. [x] Define the protocols the rest of the code depends on, and the domain events in `core/events.py`. *(they became narrow per-need protocols — `ChatSender`, `Subscriber`, `SessionLog` — instead of one `TwitchApi`, so fakes stay small)*
3. [x] Implement token persistence hooks backed by `oauth_tokens` in SQLite. *(`twitch/tokens.py`; refreshes are saved through `event_token_refreshed`)*
4. [x] Record real EventSub payload fixtures for adapter contract tests. *Done 2026-09-20: `scripts/record_eventsub.py` records notifications from Twitch's simulator into `tests/fixtures/eventsub/`, and `tests/chat/test_eventsub_contract.py` replays each one through TwitchIO's own `create_event_instance` into the adapter, so a change to the JSON or a rename inside TwitchIO fails here rather than in production. The simulator cannot trigger any `channel.chat.*` topic, so chat messages, notices and deletions stay on hand-built objects in `test_twitch.py`; follows, redemptions and cheers are recorded. Volatile ids and timestamps are pinned when recording, so re-recording against a newer CLI diffs only on real changes — and a test beside the mock server compares a live frame's shape with the committed one.*
