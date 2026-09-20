# ADR-0001: Chat transport — EventSub WebSocket to receive, Helix to send

**Status:** Accepted (implemented; see Action Items) — 2026-09-17
**Date:** 2026-09-16
**Deciders:** Project owner

## Context

The bot must read chat messages and channel events (subs, raids, stream online) and post replies. It runs on a homelab behind NAT and has no public ingress. Twitch offers three ways to get events:

- Legacy IRC (TMI) over WebSocket
- EventSub over WebSocket
- EventSub over webhooks

For sending, the bot can use either IRC `PRIVMSG` or Helix `POST /chat/messages`. Twitch's recommended path for chat bots is now EventSub plus Helix, and chat-bot features (the bot badge, `channel:bot` authorization) are built around it.

## Decision

- Receive `channel.chat.message` and all other events through **EventSub over WebSocket**.
- Send chat through the **Helix Send Chat Message** endpoint.
- Don't use IRC.

## Options Considered

### Option A: IRC (TMI)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Low. The protocol is simple and many examples exist. |
| Cost | Free |
| Scalability | Fine for one channel |
| Team familiarity | High, since it's the classic approach |

**Pros:** Simple. One connection covers both reading and writing. Chat gets no separate authorization.
**Cons:** It is a legacy path, and new chat features land in EventSub first. Non-chat events (subs, raids, online) still need EventSub, which means two transports. The bot can't get the chat bot badge or the `channel:bot` model.

### Option B: EventSub WebSocket + Helix send (chosen)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium. The bot has to handle the session welcome, keepalive, reconnect messages and resubscribing. The library absorbs most of this. |
| Cost | Free |
| Scalability | Well within the per-connection subscription limits for a few channels |
| Team familiarity | Medium |

**Pros:** One transport covers chat and all channel events. Connections are outbound only, which suits NAT. Messages arrive as structured JSON with badges, reply threads and fragments. This is the path Twitch supports going forward.
**Cons:** The lifecycle has more states. Sends go through an HTTP call per message. At-least-once delivery means the bot has to dedupe.

### Option C: EventSub webhooks
| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium-high. Needs public HTTPS, signature verification and challenge handling. |
| Cost | Needs a domain and tunnel or reverse proxy (Cloudflare Tunnel or similar) |
| Scalability | Best at large scale |
| Team familiarity | Low |

**Pros:** No long-lived socket. Survives brief bot restarts because Twitch retries.
**Cons:** Needs public ingress into the homelab, which adds attack surface. Twitch doesn't deliver `channel.chat.message` well for this use.

## Trade-off Analysis

B beats A because every bot of this kind eventually wants sub, raid and online events, which need EventSub anyway. With B there is one transport instead of two. B beats C because the homelab constraint (no inbound exposure) outweighs the durability of webhooks at this scale. A few seconds of missed chat during a restart is acceptable.

## Consequences

- **Easier:** adding new event types, since each one is just another subscription. Firewalling, since all traffic is outbound.
- **Harder:** the bot has to handle reconnect and resubscribe correctly, dedupe by `message_id`, and rate-limit HTTP sends itself (see the Outbox in architecture.md).
- **Revisit:** if the bot grows to many channels or needs guaranteed delivery, reconsider webhooks behind Cloudflare Tunnel.

## Action Items

1. [x] Register the Twitch app. Set redirect URI `http://localhost:8080/auth/callback`. *(done 2026-09-17)*
2. [x] Authorize the bot account (`user:read:chat user:write:chat user:bot`). *(done 2026-09-17; the token is rejected if it belongs to another account)*
3. [x] Broadcaster authorization (`channel:bot` and the full-tier scopes). *Built 2026-09-19 as `/auth/connect` (ADR-0007 item 5); not yet run against a live channel.*
4. [x] Restart the client when it stops on its own. *Built 2026-09-20: the service calls `on_stopped` from a task of its own, and `__main__` starts it again after 5 s, doubling to at most 5 min so a Twitch outage isn't met with a reconnect storm. Coming back re-runs the whole startup path, so subscriptions, capabilities, broadcaster grants and the backfill of what was missed are all redone.*
5. [ ] Build a reconnect test against the Twitch CLI mock EventSub server. *(the restart path is covered by a fake client; the real handshake isn't)*
5. [x] Add `message_id` dedupe (LRU) in the adapter. *(`TwitchService.emit`, 2000 ids)*
