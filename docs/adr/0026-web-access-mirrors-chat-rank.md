# ADR-0026: Web access mirrors chat rank

**Status:** Accepted — 2026-09-29 (amends ADR-0017)
**Date:** 2026-09-29
**Deciders:** Project owner

## Context

ADR-0017 gives a Twitch session one of two roles: `admin`, or `moderator` of a list of channels. The web
site then offers a channel's moderators less than chat does, and its broadcaster no more than its
moderators:

- a moderator can switch a published command off (`!cc disable`), search the log (`!logsearch`) and read
  what ran in chat, but the matching routes are admin-only;
- a broadcaster can turn logging and backfill on or off, set the "who may" roles and make the bot leave
  (`!part`), and none of that is open to them on the web;
- someone who moderates no channel the bot is in gets no session at all, so they can't add the bot to
  their own channel, look at the commands they published, or use the explain sandbox signed in.

The site is also moving from a hidden `/admin` area to a Manage bar that appears for anyone signed in.

## Decision

### Three session roles

`role` in `GET /api/v1/session` is `admin`, `moderator` or, new, `user`:

- `admin`: unchanged (a bot owner, a global bot admin, the password, an API key).
- `moderator`: signed in with Twitch and managing at least one joined channel, their own or one Twitch
  lists as moderated. The name stays for older sites. It covers broadcasters too, and the per-channel
  rank below says which.
- `user`: signed in with Twitch and managing none. Sign-in no longer fails with `no_channels`. The
  reason stays in the list the login page knows, but the bot doesn't produce it any more. A session that
  loses its last channel at a refresh becomes `user` rather than being signed out.

### The rank in each channel is the chat rank

A caller's rank in a channel is what chat would give them there. `PolicyService.build_chatter` works it
out with the Twitch role as the badge: `broadcaster` in their own channel, `moderator` in one they
moderate, raised by any custom role they hold there. An admin has the bot-admin rank everywhere.

`GET /session` adds:

```jsonc
{
  "channel_roles": {"vexoulz": "broadcaster", "friend": "moderator"} | null,  // null for an admin
  "channel_ranks": {"vexoulz": 100, "friend": 80} | null,
  "own_channel": {"login": "vexoulz", "joined": true, "status": "joined", "tier": "full"} | null
}
```

`own_channel` is the signed-in user's own channel, joined or not (`joined: false`, `status` and `tier`
null when the bot was never there). It is null for the password session. The site uses it to offer "add
the bot to my channel" and, when `tier` isn't `full`, "upgrade" (the ADR-0007 `/auth/connect` flow).

### Areas and minimum ranks

A private route declares an area, and a `channel` route may also declare a minimum rank:

| Area | Who |
|---|---|
| `personal` | anyone signed in, for things about themselves |
| `channel` | a caller who manages the channel in the path, at the route's minimum rank (moderator unless it says otherwise) |
| `admin` | admins |

What moves:

| Route | Before | After | Chat equivalent |
|---|---|---|---|
| `PATCH /channels/{login}/publications/{name}` | admin | moderator | `!cc disable/enable` |
| `GET /channels/{login}/runs` | admin | moderator | |
| `GET /channels/{login}/messages`, `/log`, `/log/coverage` | admin | moderator, or public (below) | `!logsearch` |
| `GET /channels/{login}/backfill` | admin | moderator | `!backfill queue` |
| `POST`, `DELETE /channels/{login}/backfill…` | admin | broadcaster | `!backfill <range>`, `!backfill cancel` |
| `DELETE /channels/{login}` | admin | broadcaster | `!part` |
| `POST /api/v1/me/channel` | (new) | personal | `!join` in the bot's channel |

`PATCH /channels/{login}` checks each field against its own minimum rank: moderator for the chat settings
and `public_log`, broadcaster for `log_enabled`, `history_backfill` and the five `*_role` fields, as in
chat. A patch with any field above the caller's rank is refused whole, as before.

Joining any channel, API keys, storage limits, the `http get` hosts and anything that writes the bot-wide
scope stay `admin`.

### Adding the bot to your own channel

`POST /api/v1/me/channel` joins the signed-in user's own channel through the same path as `!join` and
`POST /channels`, audited as the user. Signing in with Twitch is the proof that the channel is theirs. A
channel the bot left because it was banned answers 409: only an admin can rejoin it. The session gains
the channel at once rather than at the next refresh.

### Public chat logs, opt-out

A channel setting, `public_log`, is on by default. While it and `log_enabled` are on, `GET …/messages` and
`GET …/log` need no sign-in: anyone can read and search the log, as on the public channel page. Such a
reader gets messages and notifications only, without removed messages or moderation entries, and a per-
address rate limit. A moderator or the broadcaster can turn `public_log` off, and then the log is for the
channel's moderators again.

## Options Considered

| Option | For | Against |
|---|---|---|
| **Mirror chat rank** (chosen) | One rule for both ways in; custom roles count on the web as in chat | The rank has to be worked out per request |
| Keep ADR-0017's two roles and move routes | Smaller | No broadcaster tier; no place for people without channels |
| Per-feature grants on the web | Fine-grained | A second permission system to keep in step with chat |

## Consequences

- **Easier:** anyone who can do something in chat can do it on the site, and nothing more.
- **Easier:** people without a channel the bot is in can sign in, add the bot, and see their own commands.
- **Harder:** chat logs are public by default. Channels that don't want that have to turn it off.
- **Sharp edge:** a custom role grants web access only in channels the user already manages: Twitch's
  moderated-channel list, not the bot's roles, decides which channels a session has.

## Action Items

1. [x] `user` role, `channel_roles`, `channel_ranks` and `own_channel` on the session; areas with a
   minimum rank; the routes above moved; `POST /me/channel`. *(2026-09-29)*
2. [x] `public_log` setting (migration 0009), public reads of `/messages` and `/log` with a rate limit.
   *(2026-09-29)*
3. [ ] doomtp-web: the Manage bar and rank-aware access (`src/lib/access.ts`).
