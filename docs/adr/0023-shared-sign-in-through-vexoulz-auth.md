# ADR-0023: Shared sign-in through vexoulz-auth

**Status:** Accepted — 2026-09-29 (amends ADR-0017)
**Date:** 2026-09-29
**Deciders:** Project owner

## Context

ADR-0017 signs moderators in to the web admin with Twitch, straight from the bot. The bot's site is now
one of several vexoulz sites (links, VODs, status), and the VOD archive's dashboard needs a Twitch
sign-in too. Each doing its own would mean a Twitch prompt per site, and signing out of one would leave
the others signed in.

[vexoulz-auth](https://github.com/vEXOULZ/vexoulz-auth) is a small service that holds the one sign-in for
those sites. A registered backend sends the browser to its `/authorize`; someone already signed in there
comes straight back with a one-time code, and anyone else goes through Twitch first. The backend trades
the code, with its client secret, for the user and a session id, and can ask later whether that session
still stands. It keeps the user's Twitch token, so it can answer `GET /moderation/channels` for a client
allowed that scope.

## Decision

A setting, `SIGNIN_PROVIDER`, picks who signs moderators in: `twitch` (the default, ADR-0017 as it is)
or `vexoulz`. The default stays `twitch`, so anyone else running the bot is unaffected.

With `vexoulz`:

- The routes don't change: `/auth/admin/login` still starts it and `/auth/admin/callback` still ends it,
  so doomtp-web needs nothing new. The callback URL is registered with vexoulz-auth instead of Twitch.
- `VexoulzSignIn` (in `twitch/signin.py`) is a `TwitchSignIn` with three parts swapped: where the browser
  is sent, how a code is redeemed, and where the channels come from. States, `next`, the error reasons
  and the session are shared with ADR-0017.
- Roles and channels are worked out as before (`access_for`), from the list vexoulz-auth returns.
- The refresh every `REFRESH_S` also asks vexoulz-auth whether the session stands, so "sign out
  everywhere" on any site ends the bot's session within that window. As before, a check that merely
  failed keeps what the session had until the next try.
- No Twitch token is held by the bot for these sessions, and nothing new is written to the database.
- The admin password is untouched: still local-only (`ADMIN_PASSWORD_NETWORKS`), still the way in that
  needs neither Twitch nor vexoulz-auth.

Settings: `VEXOULZ_AUTH_URL` (as browsers reach it), `VEXOULZ_AUTH_INTERNAL_URL` (as the bot does;
defaults to the first), `VEXOULZ_AUTH_CLIENT_ID` (`dtp`), and `VEXOULZ_AUTH_CLIENT_SECRET` or
`VEXOULZ_AUTH_CLIENT_SECRET_FILE`. With `vexoulz` and any of those missing, Twitch sign-in is off and the
bot logs `signin.not_configured`.

## Options Considered

| Option | For | Against |
|---|---|---|
| **A setting that picks the provider** (chosen) | Same routes and sessions; other hosts keep ADR-0017 | Two code paths to keep tested |
| Always go through vexoulz-auth | One path | Every other host of the bot would need to run vexoulz-auth |
| Keep ADR-0017 and share nothing | No new service | A Twitch prompt per site; signing out of one leaves the rest |

## Consequences

- **Easier:** someone signed in on any vexoulz site reaches the bot's admin without another prompt.
- **Harder:** with `vexoulz`, sign-in depends on one more service. The password login still works
  locally when it is down, and signed-in sessions stand until it answers again.
- **Sharp edge:** signing out everywhere reaches the bot only at the next refresh (up to `REFRESH_S`).

## Action Items

1. [x] `SIGNIN_PROVIDER`, `VexoulzSignIn` and its settings, with tests through a fake vexoulz-auth.
   *(2026-09-29)*
2. [ ] Register the `dtp` client in vexoulz-auth, set the settings and `SIGNIN_PROVIDER=vexoulz` on the
   server, after vexoulz-auth is deployed.
