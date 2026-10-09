# ADR-0030: View as, scoped on the server

**Status:** Accepted, 2026-10-09
**Date:** 2026-10-09
**Deciders:** Project owner

## Context

doomtp-web lets a bot admin preview the site as someone else: signed out, a plain user, a moderator or
the broadcaster of one channel, or the holder of a custom role there. The bot still saw an admin, so the
site emulated the preview in the browser. It rewrote the answers of the reads it knew the bot scopes by
caller (`/channels`, `/audit`, `/channels/{login}/roles`), re-implemented the rule for which channels a
session lists, and hard-coded the ranks. Every new route that answers by caller showed admin data in a
preview until the site learned another path. The rules that decide what a caller sees are the bot's
(ADR-0017, ADR-0026), so the preview belongs here too.

## Options

1. **Keep the emulation in the site.** Nothing to build here, and it drifts with every route.
2. **A preview session.** The admin mints a second, read-only session as the viewer. Two cookies, and
   the site has to juggle which one each request carries.
3. **A request header on the admin's own session.** Each request says who to answer as. Nothing is
   stored, and dropping the header ends the preview.

## Decision

Option 3. An admin session may send

```
X-View-As: signed-out | user | moderator@<channel> | broadcaster@<channel> | <rank>@<channel>
```

where `<rank>` is a custom role's, 1 to 99, held by someone who isn't a Twitch moderator there. The value
is case-insensitive and a `#` before the channel is ignored.

- **Reads** are answered as that viewer would get them. `access.authenticate` turns the admin into the
  caller that viewer's own session would make: role `moderator` with the one channel for a moderator or
  the broadcaster, role `user` with none otherwise, and the previewed rank in that channel only. Every
  route goes through it, so a new route is scoped without anyone remembering to. The admin's user id and
  actor stay, so their own changes (`actor=me`, a user's audit) and personal pages are still theirs, as
  in the site's emulation.
- A custom role does not list the channel, as with a real sign-in: Twitch decides who manages a channel,
  and a role only raises a rank there (ADR-0026).
- `GET /api/v1/session` answers with that viewer's session (`role`, `channels`, `channel_roles`,
  `channel_ranks`, `own_channel`, the broadcaster's own being the previewed channel) and adds
  `view_as`, the header's value as the bot read it.
- **Signed out:** a private read answers 401 and carries `X-View-As: signed-out` in the response, so the
  site can tell it from its session ending; public routes answer as for anyone, and `GET /session` says
  `authenticated: false`.
- **Every write is refused** with 403 ("read-only while viewing as ..."), before the CSRF check. So are
  the API key routes, which no previewed viewer could use.
- The header is a 403 from a session that isn't an admin's, a 400 with an API key, and a 400 when its
  value doesn't parse or names a channel the bot doesn't know.

## Consequences

- doomtp-web drops `auth.scope`, `projected()` and its copy of the ranks, sends the header while
  previewing, and reads the previewed session from `GET /session`.
- A preview is exact for what the bot decides, including a broadcaster's `manageable` roles and
  `your_rank`.
- Nothing about a preview is stored; a request without the header is the admin's again.

## Action Items

1. [x] `X-View-As` in `api/access.py`: reads scoped, writes refused; `GET /session` and the key routes
   follow it. Tests in `tests/test_api_view_as.py`.
2. [x] Documentation: architecture §11, this ADR.
3. [ ] doomtp-web sends the header and drops its emulation (vEXOULZ/doomtp-web#67).
