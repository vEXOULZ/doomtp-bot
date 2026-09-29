# ADR-0020: A gated HTTP query primitive

**Status:** Accepted — 2026-09-28
**Date:** 2026-09-28
**Deciders:** Project owner

## Context

ADR-0019 left `weather` and similar commands waiting for a way to read a web API from the language, and
asked for this ADR before any code. Today the only outbound HTTP the bot makes is its own: Helix, the
recent-messages service (ADR-0008) and Twitch sign-in. None of those takes a URL from a user.

A command that fetches a URL someone typed or stored is the riskiest thing the language could offer:

- **SSRF.** The bot runs next to Postgres, its own API and whatever else is on the host's network. A
  URL that resolves to `127.0.0.1`, `10.0.0.5`, `169.254.169.254` or `[::1]` must never be fetched,
  including through redirects and DNS answers that change between the check and the connect.
- **Abuse of third parties.** A popular channel's command could turn chat into a load generator against
  a site that never agreed to it.
- **Content.** Whatever comes back ends up in chat, so it passes the content filter and the link rule
  like any other output.
- **Latency.** A run has a time budget (spec §6.4). A slow host must not hold a pipeline hostage.

## Decision

A primitive **`http`** command in a new `http` module. Only bot admins can write code that calls it.

### Who can use it

- **Only from derived commands published by a bot admin.** `http` runs only inside the body of a custom
  command whose publisher is a bot admin at the time of the run. Typed in chat, in a trigger or callback,
  or in anyone else's custom command, it fails with `E_HTTP_NOT_ALLOWED` before any request is made. It
  is hidden from `help`. So every URL the bot ever fetches was written by an admin; a chatter can only
  fill in the parts the admin's body puts `{arg…}` placeholders in.
- **A global allow-list of hosts**, set with `!admin http allow|deny|list <host>` (and a JSON endpoint
  for the admin UI in doomtp-web). A host matches exactly; a leading `*.` matches one or more
  subdomains. There is no "allow everything". The list still matters when an admin wrote the body,
  because the placeholders in it are chatter input.
- **Channels choose the commands, not the primitive.** A channel turns an admin's derived command such as
  `weather` on or off like any other. A bot admin can still `!module disable http` for one channel or
  globally.
- **Secrets are admin settings, never variables.** An API key is stored per host with `!admin http secret
  <host> query <param> <value>` or `!admin http secret <host> header <name> <value>`, and cleared with
  `!admin http secret <host> clear`. `http` attaches it to every request to that host. It is write-only:
  nothing in the language, `!admin`, the API or the logs ever shows it back, only that a secret is set.
  A key therefore can't be read by a placeholder, echoed, stored in a variable or leaked by a URL someone
  typed. The chat line that sets it is deleted when the bot is a moderator, and the JSON endpoint is the
  recommended way to set one.

### What it does

`http get <url> [path]`:

- **GET only**, HTTPS only (HTTP only for hosts an admin marks as such). No request body, no cookies,
  and no headers except the host's secret header, if one is set. The user agent names the bot.
- **Timeout** 3 s for the whole request, inside the run's own budget.
- **Size cap** 128 KB of response body. A bigger body fails instead of being truncated.
- **JSON only.** The response must parse as JSON; `path` is a bracket path in ADR-0018's syntax
  (`[current][temp_c]`, `[list][0][name]`) that picks one value out. The value becomes the result's data;
  its text form becomes the message.
- **Redirects** are followed at most 3 times, and each hop is checked against the allow-list and the
  address rules again.

### Address rules (SSRF)

- The host is resolved once by the bot, every resolved address is checked, and the connection is made
  to the checked address (with SNI and `Host` set to the name). Loopback, private (RFC 1918, RFC 4193),
  link-local, CGNAT (100.64/10), multicast, unspecified and reserved ranges are refused, for IPv4, IPv6
  and IPv4-mapped IPv6.
- IP literals in the URL are refused outright; only names on the allow-list are fetched.
- Ports other than 443 (and 80 where HTTP is allowed) are refused.

### Limits

- **Rate limits:** per channel, 10 requests a minute; per host across all channels, 60 a minute. Both
  are admin settings. Over the limit fails with code 125 (upstream limited), as other upstream limits do.
- **Caching:** identical GETs within 60 s share one response, which also softens a chat that spams a
  command.

### Errors

A new block, 400–499, as ADR-0018 reserved it:

| Error | Code | When |
|---|---|---|
| `E_HTTP_NOT_ALLOWED` | 400 | not called from an admin-published derived command, the host isn't on the allow-list, or the scheme or port isn't allowed |
| `E_HTTP_ADDRESS` | 401 | the name resolves to a refused address |
| `E_HTTP_TIMEOUT` | 402 | no full response within the timeout |
| `E_HTTP_TOO_BIG` | 403 | the body is over the size cap |
| `E_HTTP_STATUS` | 404 | the server answered with a non-2xx status (the status is in the data) |
| `E_HTTP_NOT_JSON` | 405 | the body isn't JSON |
| `E_HTTP_PATH` | 406 | the path doesn't exist in the response |
| `E_HTTP_UNREACHABLE` | 407 | the connection failed: refused, reset, or a TLS error *(added 2026-09-28)* |

### Logging

Every request is logged to `command_runs` like any other invocation, with the host, the status, the
size and the time taken, but never the query string or the secret header. The query string may carry
a host's secret, and a chatter's arguments besides.

## Options Considered

### Option A: no HTTP at all
**Pros:** nothing to secure. **Cons:** `weather`, stock-style readouts and game APIs stay impossible, or
each becomes its own Python primitive with its own key handling.

### Option B: a gated, read-only JSON GET, written by admins only (chosen)
**Pros:** one reviewed code path covers every read-only API. Admins decide which hosts exist and write
every body that reaches them. **Cons:** SSRF protection has to be right, and its tests have to prove it.

### Option C: one Python primitive per service (`weather`, …)
**Pros:** each is narrow and easy to reason about. **Cons:** every new service is code, a release and an
API key the bot owns, which is the opposite of ADR-0019's rule that only what the language can't express
is primitive.

## Consequences

- **Easier:** `weather` becomes a derived command an admin publishes, such as `http get
  https://api.example/weather?q={arg.1} [current][temp_c] | echo {_1}°C in {arg.1}`, with the API key
  attached from the host's secret. Each channel turns it on or off.
- **Narrower:** channels and ordinary publishers can't build their own HTTP commands. If that is wanted
  later, it needs a new ADR, per-channel allow-lists and a way to keep secrets away from their bodies.
- **Harder:** the bot gains an outbound path influenced by users. The address checks, the redirect rule
  and the rate limits need tests that try to break them.
- **Depends on** ADR-0018 item 3 (bracket paths) for `path`, and on ADR-0018 item 1 for the numbered
  error block.

## Review (2026-09-28)

The open questions were settled by the project owner:

1. **API keys** are a separate, write-only admin setting per host, not something a channel stores.
2. **The size cap** is 128 KB, not 64 KB.
3. **`http` runs only from derived commands**, and only when the command's publisher is a bot admin.
   That also replaced the per-channel allow-lists of the proposal with a single global one.

## Action Items

1. [x] Settle the open questions and accept or reject this ADR. *(2026-09-28)*
2. [x] The `http` module with `http get`, the admin-publisher check, the address rules and redirects, with tests against a local
   server that tries each SSRF trick. *(2026-09-28: `modules/httpget.py`, `webfetch/`, `tests/test_http.py`)*
3. [ ] The host allow-list and secrets: the tables, `!admin http allow|deny|list|secret`, and the JSON
   endpoints (plus a doomtp-web PR).
4. [x] Rate limits, the 60 s cache and the `E_HTTP_*` codes in `runtime/result.py` and spec §6.2. *(2026-09-28)*
5. [ ] A `weather` derived command in the starter pack, off until a channel allows its host.
