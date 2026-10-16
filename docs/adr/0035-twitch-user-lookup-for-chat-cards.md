# ADR-0035: A Twitch user lookup for the sites' chat user cards

**Status:** Accepted, 2026-10-16
**Date:** 2026-10-16
**Deciders:** Project owner

## Context

The sites' chat views (doomtp-web's chat log, the VOD sites' chat replay) open a user card when a chatter's
name is clicked. It shows the chatter's profile picture, Twitch id, account age, partner/affiliate/staff, and
bio. Helix has those but needs a token, so a browser can't ask Twitch itself. The chat lines carry the
chatter's id when they come from the bot (the log, the VOD sites' live chat), and only a display name when
they come from Twitch's VOD replay.

## Options

### A. A third-party public API (ivr.fi) from the browser

**Pros:** no backend work. **Cons:** every viewer's browser depends on a service we don't run. The owner
asked for an endpoint of our own instead (2026-10-16).

### B. An endpoint in each site's backend

**Pros:** same origin everywhere. **Cons:** twitch-archive and the bot would each carry the same lookup and
cache.

### C. One endpoint in the bot, shared with the VOD sites' origins

**Pros:** one cache and one Helix client, which the bot already has. **Cons:** the VOD sites call another
origin, so the route needs CORS.

## Decision

**C.** `GET /api/v2/twitch/users?id=<id>` or `?login=<login>`. The id is used when both are given.
The answer is `{id, login, display_name, type, broadcaster_type, description, profile_image_url,
created_at}`: one Helix `GET /users` with the app token, so never the email Helix adds for a user token's
own account. A user Twitch doesn't return (renamed, deleted, suspended) is a 404.

- Anyone may ask, signed in or not, because the VOD sites' viewers are signed out. Only lookups that reach
  Twitch count against a limit, 60 a minute per client address.
- Answers are cached in the process: an hour for a user, ten minutes for "no such user", 5000 entries. A user
  found by id is also cached under their login, and the other way round.
- `USER_LOOKUP_ORIGINS` lists the other origins that may read it. They get `Access-Control-Allow-Origin` on
  every answer of this route, errors included. No other route is shared, and no cookie is read, so a
  simple GET needs no preflight.
- Follower counts and chat colours are left out. twitchio asks for a broadcaster token for followers, and
  the chat line already has the colour.

## Consequences

- The production `.env` sets `USER_LOOKUP_ORIGINS` to the VOD sites' origins.
- vex-platform-web's `UserCard` reads this route through the `userLookup` a site gives `createPlatformUi`.
  doomtp-web calls it on its own origin, and the VOD sites call it on the bot's.
