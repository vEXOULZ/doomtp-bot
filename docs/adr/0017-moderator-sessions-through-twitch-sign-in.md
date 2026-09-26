# ADR-0017: Moderator sessions through Twitch sign-in

**Status:** Proposed — the session shape needs agreeing with `doomtp-web` before any code
**Date:** 2026-09-26
**Deciders:** Project owner

## Context

The API has two kinds of caller (architecture §11): an **API key** (`read` or `write`, for scripts) and
the **admin session** from the admin password, which can do everything. ADR-0016 left a third one for
later: a person signing in with Twitch.

`doomtp-web` already has two views (`src/lib/access.ts`): an admin view and a moderator view that hides
what a moderator shouldn't touch. Today only the admin view is reachable, through the password. The next
step for the site is Twitch sign-in, so that a channel's moderators and broadcaster can manage that
channel without being handed the admin password.

Hiding things in the site is not access control. Whatever a moderator session may not do, the API must
refuse.

## Decision (proposed)

### The session says who is behind it

`GET /api/v1/session` gains three fields. The existing ones stay as they are.

```jsonc
{
  "authenticated": true,
  "csrf": "…",
  "expires_at": 1790000000000,
  "admin_enabled": true,
  "role": "admin" | "moderator",           // new; null when not authenticated
  "user": {"id": "1001", "login": "vexoulz"} | null,   // new; null for the password session
  "channels": ["vexoulz", "friend"] | null  // new; null means every channel (an admin)
}
```

- **The password session** is `role: "admin"`, `user: null`, `channels: null`, exactly what it can do
  today.
- **A Twitch session** is `role: "admin"` when the user is a bot owner (`BOT_OWNER_IDS`) or a global bot
  admin (`!admin add`), and `role: "moderator"` otherwise. `channels` lists the joined channels the user
  owns or moderates. A user with none gets no session: signing in says so instead.
- **Where `channels` comes from:** their own channel if the bot is in it, plus Helix
  `GET /moderation/channels` (scope `user:read:moderated_channels`) filtered to channels the bot is in.
  It is read at sign-in and refreshed at most every few minutes, so a moderator who loses the role
  loses access within that window, not at once. The session keeps it in memory with everything else.

### What a moderator session may use

Checked on the server for every request, against the channel in the path:

| Area | Moderator (own channels only) | Admin |
|------|------------------------------|-------|
| Modules, command rules, triggers, filters, ignored users | read and write | yes |
| Channel settings: `prefix`, `quiet_errors`, `cc_edit_notice`, `reply_hold_ms`, `timezone`, `automod_*` | read and write | yes |
| Channel settings: the "who may" roles (`*_role`), `log_enabled`, `history_backfill` | read only | yes |
| Publications, variables | read (writes as chat allows) | yes |
| `POST /explain`, including `as_user` | in their channels | yes |
| `/audit?channel=` | that channel's entries | yes |
| `/audit` without a channel, `/channels` listing | only their channels | yes |
| Join, part, rejoin | no | yes |
| API keys, `/runs`, `/messages` (log search), health | no | yes |

`403` for a channel outside `channels`, and for a field or endpoint the table rules out. A `PATCH
/channels/{login}` that mixes allowed and refused fields is refused whole, so nothing half-applies.

### Audit

A Twitch session writes with `Actor(user_id, "web")`: `actor_user_id` is the signed-in user and `via` is
`"web"`. The password session keeps `Actor(None, "api")`. A self-ignore can then be lifted from the site
by the same rule chat uses: `DELETE /channels/{login}/ignored/{user_id}` is allowed to any signed-in user
whose id is `user_id`, when the entry's `added_by` is also that id (§5.4).

### Groundwork, before the sign-in itself

1. `_authenticate` in `api/routes/data.py` returns a `Caller` (`role`, `user_id`, `channels`,
   `actor`) instead of a string, and every route takes the `Actor` from it instead of the module-level
   `ACTOR`. Keys and the password session map to what they are today, so nothing changes for them.
2. One `require_channel(caller, settings, area)` check per route, driven by the table above, so the
   table is code in one place, and a test walks every route with a moderator caller.
3. Then the OAuth flow under `/auth/` (it already hosts the bot and broadcaster flows), the session
   store keyed by Twitch user, and the three new `/session` fields.

## Options Considered

### Option A: one role with per-channel scopes on API keys
**Pros:** no new login. **Cons:** a key per moderator, handed out and revoked by hand, and keys are for
scripts: they sit in a browser's storage and never expire.

### Option B: Twitch sign-in with a moderator role checked on the server (proposed)
**Pros:** access follows Twitch's own moderator list, and the audit log names the person.
**Cons:** another OAuth flow, another scope to ask for, and the moderator list is only as fresh as the
last refresh.

### Option C: Twitch sign-in, but the site enforces the moderator view
**Cons:** anyone can call the API directly. Not an option; listed so nobody suggests it later.

## Consequences

- **Easier:** moderators manage their channel without the admin password, and every change says who
  made it.
- **Harder:** every route now has to say who may use it; a new route without that check should fail a
  test, not quietly allow everyone.
- **Sharp edge:** sessions are in memory. A restart signs everyone out, as it does for the password
  session today.
- **Open questions to settle with `doomtp-web`:** whether `channels` should carry more than logins (the
  sign and the tier would spare a request per channel), and whether a moderator sees `/runs` for their
  own channel.

## Action Items

1. [ ] Agree the `/session` shape and the table above with `doomtp-web`, then mark this ADR accepted.
2. [ ] `Caller` from `_authenticate`, the `Actor` taken from it, and `require_channel` with a test over
   every route.
3. [ ] Twitch sign-in under `/auth/`, sessions keyed by Twitch user, and the new `/session` fields.
4. [ ] Signed-in users may delete their own self-ignore.
