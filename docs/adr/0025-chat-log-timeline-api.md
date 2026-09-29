# ADR-0025: The chat log as a paged timeline on the API

**Status:** Accepted — 2026-09-28 (drafted as ADR-0023; renumbered when ADR-0023 went to the shared sign-in)
**Date:** 2026-09-28
**Deciders:** Project owner

## Context

The bot logs every message, notification and moderation event in the channels it joins, deletions
included (architecture §3.1), and knows exactly when it was listening (`log_sessions`) and which holes
backfill filled (ADR-0008). The API shows almost none of it: `GET /channels/{login}/messages` is a
full-text search that returns the newest 500 matches and stops, and `/runs` is the newest command runs.

Two readers now want the log itself:

- **The web site** (doomtp-web, ADR-0016) wants a log viewer: open on the newest lines, page back, narrow
  to a window, a chatter or a search, and show what moderators removed and why.
- **The VOD archive** (twitch-archive) replays a stream's chat from what Twitch's VOD comments give it.
  Those lack what the bot saw: which messages were deleted or cleared by a timeout, the timeouts and bans
  themselves, subs and raids, and which bot reply answered which command. The archive wants a stream's
  window, oldest first, all of it, and to know whether the bot's log of that window is whole.

Both are reads over the same rows. The question is how to cut them into pages and what to put in each.

## Decision

Two admin-only routes over the `chatlog` schema, in `chatlog/timeline.py`:

- **`GET /api/v1/channels/{login}/log`** — one **timeline** of entries of three kinds, merged in time
  order: `message`, `notification` and `moderation`. Filters: `since` (inclusive) and `until` (exclusive)
  in ms, `kind` (repeatable), `user` (a login, matched through `user_names`, so a renamed chatter is
  still found), `q` (the same full-text search as `/messages`; messages only), `hide_removed` (leave out
  deleted and cleared messages, as chat shows them). `order` is `desc` by default, as a viewer opens, or
  `asc`, as a replay walks.
- **Keyset pages.** A page is at most `limit` entries (500 at most) and a `next` cursor, `null` at the
  end. The cursor is the last entry's `(at, kind, id)`, opaque to the caller, and pages only in the
  order it was read in. Each kind is read from its own `(channel_id, time)` index with the cursor's
  predicate and `limit + 1` rows, and the three are merged; a deep page costs what the first one does,
  and rows written while someone pages don't shift what they see.
- **Entries carry what a replay needs.** A message has its user, text, fragments and badges (parsed,
  not JSON strings), bits, reply parent, the `deleted_at`/`cleared_at` flags and, for a command or a bot
  reply, the **run** that links them: a command's run from `command_runs.trigger_id`, a reply's through
  `outbound_msgs.twitch_message_id` → `run_ref`. The run's `trigger_id` on a reply is the message that
  asked for it. A moderation entry carries the target and moderator with their logins, duration and
  reason; a notification its type and payload.
- **`GET /api/v1/channels/{login}/log/coverage?since=&until=`** — the log sessions that overlap the
  window and every **gap** in it, clipped to it: `between_sessions` (with the backfill run that covered
  it, if any), `before_log` (before the bot ever listened) or `not_listening` (after the last session
  ended). `complete` is true when every gap was filled by a complete backfill run. The archive asks this
  before trusting the absence of a message.
- **Admins only**, like `/messages` and `/runs` (ADR-0017): an API key or an admin session. The archive
  uses a `read` key.

Message ids are Twitch's own UUIDs, the same over EventSub and the IRC backfill. How the archive joins
them to its comments (by id where they match, otherwise by time and user) is the archive's decision, in
its own repository.

## Options Considered

### Option A: offset pages over each table, one route per kind
**Pros:** the obvious REST shape; each route is a plain `SELECT … OFFSET`. **Cons:** a deep offset scans
everything before it, and rows arriving while someone pages shift the pages under them. Both readers
would merge three lists themselves to draw one timeline.

### Option B: one merged timeline with a keyset cursor (chosen)
**Pros:** one call gives a replay everything for a window in the order it happened; each page is an index
range scan; a cursor is stable against inserts. **Cons:** the cursor has to break ties between kinds at
the same millisecond, and the route does a small merge in Python.

### Option C: export a window as a file (JSON lines) for the archive, and keep the viewer on search
**Pros:** the archive gets one response per VOD. **Cons:** two ways to read the same rows, and the viewer
still has nothing to page with. A replay of a long stream is tens of thousands of messages, which a
cursor walks just as well.

## Consequences

- **Easier:** a log viewer on the site; a chat replay that shows deletions, timeouts, subs and which reply
  answered which command; checking whether a stream's window was fully logged.
- **Harder:** the entry shapes are now a contract with two other repositories. Adding a field is free;
  renaming or removing one needs both readers changed first.
- `/messages` stays for the search it already serves. `/log` with `q` is the paged version of it.
- Nothing new is stored and no index is added: every read uses the `(channel_id, time)` indexes the
  `chatlog` schema already has, and the run lookup is bounded to the page's time range.
- **Revisit:** moderators reading their own channels' logs. ADR-0017 kept the log for admins; Twitch
  shows moderators the same messages in its mod view, so opening `/log` to them would be one area change
  on the route, if asked for.

## Action Items

1. [x] `GET /channels/{login}/log` and `/log/coverage` in `chatlog/timeline.py`, with tests. *(2026-09-28)*
2. [ ] A log viewer on the channel's admin page in doomtp-web.
3. [ ] An enrichment step in twitch-archive that reads a VOD's window from `/log` and checks `/log/coverage`.
