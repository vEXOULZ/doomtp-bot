# ADR-0007: Channel access tiers — basic (no broadcaster action), moderator, full

**Status:** Proposed
**Date:** 2026-09-16
**Deciders:** Project owner

## Context

The bot should be usable in channels where the broadcaster hasn't authorized it (**basic usage**), and it should unlock more when the broadcaster mods it or connects it via OAuth (**full usage**). We need to know which Twitch features work at each level. Everything should use the bot's single user token over EventSub WebSocket (ADR-0001).

### Research findings (Twitch docs, checked 2026-09-16; recheck before implementing)

- **Reading chat without broadcaster permission works.**
  - `channel.chat.message`: *"Requires `user:read:chat` scope from the chatting user. If app access token used, then additionally requires `user:bot` scope from chatting user, and either `channel:bot` scope from broadcaster or moderator status."*
  - The same wording applies to `channel.chat.notification`, `channel.chat.message_delete`, `channel.chat.clear` and `channel.chat.clear_user_messages`.
  - With a **user access token over WebSocket**, the bot therefore only needs its own `user:read:chat`. `channel:bot` is required only on the app-token path (webhooks/conduits).
- **Sending chat without broadcaster permission works.**
  - Send Chat Message requires `user:write:chat`.
  - The extra `user:bot` + (`channel:bot` or moderator) requirement applies only when using an **app access token**.
  - Channel chat rules still apply: followers-only, subs-only, slow mode, verified email or phone, and bans.
- **The chat bot badge** comes with the app-token plus `channel:bot` path, which means the full tier.
- **No auth needed:** `stream.online`, `stream.offline` and `channel.raid`. However, WebSocket subscriptions share a **`max_total_cost` of 10** per user token. Subscriptions for channels that haven't authorized the app cost 1 each, so they don't scale past a handful of channels. Twitch also allows max 3 WebSocket connections and 300 subscriptions per connection.
- **Moderator scopes needed:** `channel.follow` (`moderator:read:followers`), `channel.moderate` (moderator read scopes), and timeouts, bans and deletes (`moderator:manage:*`).
- **Broadcaster scopes needed:** channel point redemptions (`channel:read:redemptions`), `channel.subscribe` (`channel:read:subscriptions`), cheers (`bits:read`) and similar.
- A channel that bans the bot returns **403** on subscription creation or send.

## Decision

Onboarding has three tiers, detected automatically and stored as capabilities per channel.

| Tier | Requirements | Subscriptions / capabilities |
|------|--------------|------------------------------|
| **basic** | Bot token `user:read:chat user:write:chat`. **No broadcaster action.** | `chat.message`, `chat.notification` (subs, resubs, gift subs, raids, announcements, bits badge tier…), `chat.message_delete`, `chat.clear`, `chat.clear_user_messages`, `send`. Stream online/offline comes from **Helix `Get Streams` polling** (batched 100 IDs, every 60 s) instead of EventSub, to stay within the cost limit. |
| **moderator** | The broadcaster mods the bot. The bot token adds `moderator:read:followers`, `moderator:manage:banned_users`, `moderator:manage:chat_messages` and the moderator read scopes for `channel.moderate`. | Adds `channel.follow`, `channel.moderate` (who and why for mod actions), timeout, ban and delete actions, higher send limits, and exemption from slow and follower modes |
| **full** | The broadcaster OAuths at `/auth/connect` with `channel:bot`, `channel:read:redemptions` (or `manage`), `channel:read:subscriptions` and `bits:read` | Adds redemptions, sub and cheer EventSub events, the chat bot badge, and cost-0 `stream.online`/`offline` subscriptions |

**CapabilityProbe:**
- **When it runs:** at join, hourly, and on any 401 or 403.
- **How it probes:** it checks mod status through the Helix moderated-channels lookup for the bot user, checks the broadcaster token's scopes when one is present, and attempts the subscriptions.
- **What it stores:** `channels.capabilities` (a JSON set) and `channels.tier`.

**Features declare their requirements.** For example, `automod` requires `moderate`, and redemption triggers require `redemptions`. When a requirement is unmet, the feature is disabled with a reason shown in `!explain`, `!help` and the admin UI.

**Joining:**
- `!join` typed by the broadcaster in the bot's own channel joins *their* channel at the basic tier.
- `!join <channel>` by a bot owner or admin joins any channel.
- `!part` leaves.
- A 403 marks the channel `banned`, unsubscribes and stops sending.

**Basic-tier etiquette:** no timers, alerts or unprompted messages until a channel mod opts in.

## Options Considered

### Option A: Tiered with auto-detection (chosen)
**Pros:** The bot is useful in any channel immediately. It upgrades smoothly as the broadcaster grants more. Features fail with a visible reason instead of silently.
**Cons:** Capability tracking and probing code, plus a polling path for stream status.

### Option B: Require broadcaster OAuth for every channel
**Pros:** One code path, the bot badge everywhere, cost-0 subscriptions.
**Cons:** Fails requirement F18 (basic usage without broadcaster involvement). High friction.

### Option C: IRC for basic channels, EventSub for full
**Pros:** IRC joins are familiar and free of cost limits.
**Cons:** Two transports (rejected in ADR-0001). Unnecessary, because EventSub with a user token already covers basic chat.

## Trade-off Analysis

The research shows EventSub with a user token already gives basic-tier chat without IRC, so A costs little extra. The only gap is stream status past the WebSocket cost limit, and one batched Helix poll per minute covers it. Keeping the tier model explicit lets modules and triggers state their needs declaratively.

## Consequences

- **Easier:** onboarding (just `!join`), clear error reporting, gradual trust.
- **Harder:**
  - Capabilities can change underneath the bot, for example when it's un-modded, so modules must react to capability-change events.
  - Basic-tier moderation data is thinner: `clear_user_messages` shows *that* a user was cleared, not *who* did it or why.
- **Etiquette and ToS:** joining channels uninvited can look like spam. Joins require a request from the broadcaster, a mod or the bot owner.
- **Revisit:**
  - When the channel count grows past about 3×300 subscriptions, or if chat events ever start counting toward cost, move to Conduits with an app token. That requires full-tier authorization per channel.
  - Verify the lead moderator badge and permissions for a `lead_moderator` capability.

## Action Items

1. [x] Spike: subscribe to `channel.chat.message` for an unrelated test channel with only `user:read:chat` over WebSocket, and confirm it works. *Verified 2026-09-17: the bot joined `#developmenttopyramidsbot` (not modded, no broadcaster OAuth), received chat and all five chat subscriptions succeeded.*
2. [x] Spike: send with only `user:write:chat` to a channel where the bot isn't a mod, and confirm the behavior and rate limits. *Verified 2026-09-17: replies were delivered there with a user token and no badge. Rate limits are not load-tested yet.*
3. [ ] Build the CapabilityProbe, the `channels.capabilities` column, and feature `requires` declarations.
4. [ ] Build the Helix `Get Streams` poller for basic-tier stream status.
5. [ ] Build the `/auth/connect` broadcaster flow and per-broadcaster token storage.
