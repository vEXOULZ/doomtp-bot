# ADR-0016: The web UI moves to a separate site over the JSON API

**Status:** Accepted (in progress; see Action Items) — 2026-09-25
**Date:** 2026-09-25
**Deciders:** Project owner

## Context

The bot's pages are server-rendered Jinja inside the bot's own FastAPI app (architecture §11). That was
the smallest thing for one Python maintainer, and it still works. But the bot is one of three sites
(`vexoulz.net`, `vods.vexoulz.net` and this one), and the other two now share one design and one stack:
Vue and TypeScript on a shared component package, each site built separately and published as static
files. The bot's pages have their own inline CSS, and every page change ships as a new bot image, which
means a bot restart.

Architecture §11 already expected this: "a SPA … can replace the pages without API changes". The JSON
API covers most of what the pages show, but not all of it. The pages read some state straight from the
app (the channels on the home page, packs, the grammar, an `!explain` report), and the admin login is a
form post that only works for a page the bot rendered itself.

## Decision

- **A separate repository, `doomtp-web`,** holds the pages: Vue and TypeScript on the shared design package,
  built to static files. The bot does not build, contain or serve it.
- **Same origin.** The site is served from the same host as the bot's `/api/*`, `/auth/*`, `/static/*`
  and `/healthz`, with the reverse proxy sending those paths to the bot and everything else to the static
  build. The browser then needs no CORS, and the session cookie stays a plain same-origin cookie. How the
  proxy is set up is a deployment matter and isn't described here.
- **The JSON API grows what the pages still read directly.** Nothing new is exposed: every public read
  already appears on a public page, and every admin read or write already has an admin page.
  - `GET/POST/DELETE /api/v1/session`: the admin login, with the same password and the same in-memory
    sessions as the admin pages. The cookie is `HttpOnly`, `SameSite=Lax`, and `Secure` over https. The
    page reads the CSRF token from `GET /session` and sends it in `X-CSRF-Token`, which the data API
    already requires of cookie writes. Logging out needs the token too.
  - `GET/POST/DELETE /api/v1/keys`: API keys, **with a session only.** A key that could mint keys would
    turn one leaked `write` key into a permanent one. The secret is in the response that creates it and
    nowhere else, as on the admin page.
  - Public: `GET /api/v1/site` (version, prefix, the channels the home page lists),
    `/site/channels/{login}` (a channel page's header), `/roles`, `/grammar`, `/explain/{token}`, `/packs`
    and `/channels/{login}/packs`. Custom commands carry their declared parameters, and `/commands`
    says which built-ins are always on or have a fixed policy, as the command table shows.
  - Admin reads: `/channels/{login}/modules` and `/channels/{login}/ignored`.
  - Health needs nothing new: `/readyz` already reports each component.
- **Failed logins are rate-limited per client address** (five in five minutes), for the JSON login and the
  admin form together. Once the pages are public, the password is the only thing in front of `/admin`.
  The address is only right behind a proxy that uvicorn trusts, so `WEB_FORWARDED_ALLOW_IPS` names those
  proxies (default: `127.0.0.1`, uvicorn's own default).
- **The Jinja pages stay until the new site covers every one of them,** then they are retired in a later
  release. `/auth/*` (bot OAuth, broadcaster connect) and `/static/*` (the editor bundle, the railroad
  diagrams) stay in the bot either way.

## Options Considered

### Option A: keep the Jinja pages and restyle them
**Pros:** no new repository, no API work, one deploy.
**Cons:** the shared components and design would be copied into templates and inline CSS and drift from
the other two sites. Every wording fix would still restart the bot.

### Option B: the SPA inside this repository, served by the bot
**Pros:** one repository, and the bot serves its own UI.
**Cons:** Node tooling for the whole UI in a Python project that keeps Node to one committed editor bundle
on purpose (ADR-0011), and still a restart per page change.

### Option C: a separate site over the JSON API (chosen)
**Pros:** the pages ship on their own schedule with the other sites' design and tooling. The API becomes
the only way in, so the bot has one contract to keep, not two.
**Cons:** a second repository to keep in step. A page that needs new data waits for a bot release. The
login becomes an API that the internet can reach, which is why logins are now rate-limited.

## Consequences

- **Easier:** page changes no longer touch the bot. The API is complete enough that any other client (a
  script, a phone shortcut) sees everything the pages do.
- **Harder:** the API is now a public contract for another repository. Renaming a field breaks `doomtp-web`,
  so fields are added, not changed, and removals go through a release of both.
- **Sharp edge:** a wrong `WEB_FORWARDED_ALLOW_IPS` either trusts clients to choose their own address,
  which defeats the limit, or lumps every login behind the proxy into one address, so one person's typos
  lock everyone out. Set it to the proxy's address, nothing wider.
- **Revisit:** the Twitch login in the plan for the sites (a per-user dashboard) adds a new kind of caller
  beside the session and the key (architecture §11). ADR-0017 proposes it.

## Action Items

1. [x] `/api/v1/session` and `/api/v1/keys`, with a login limit shared with the admin form, and
   `WEB_FORWARDED_ALLOW_IPS`. *(2026-09-25)*
2. [x] The public reads (`/site`, `/roles`, `/grammar`, `/explain/{token}`, `/packs`,
   `/channels/{login}/packs`) and the admin reads (`/channels/{login}/modules`, `/ignored`).
   *(2026-09-25)*
3. [ ] `doomtp-web` covers every page: home, commands, language, features, channel, explain, login, admin,
   admin channel and admin explain.
4. [ ] Stop serving the Jinja pages and remove `webui/pages.py` and its templates, keeping `/auth/*` and
   `/static/*`.
5. [ ] Rewrite the "UI technology" part of architecture §11 for the new site once item 4 lands.
