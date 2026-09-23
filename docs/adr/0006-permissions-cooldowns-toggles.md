# ADR-0006: Ranked roles, dual cooldowns with callbacks, layered toggles

**Status:** Accepted, revision 2 (implemented; see Action Items) — 2026-09-17
**Date:** 2026-09-16
**Deciders:** Project owner

## Context

- Commands are gated by tier. The tiers are Twitch built-ins (broadcaster, lead moderator, moderator, VIP, subscriber) and custom roles such as `ambassador`.
- Custom roles may rank **above moderator**, for example a lead-mod-like or co-streamer role.
- **Global bot owners and bot admins** sit above all other roles in every channel.
- Cooldowns apply **per tier** (a shared timer) **and per user**, and **both** must have expired.
- Denials and cooldowns are **silent**, with an optional **callback** for custom responses.
- Modules and commands can be toggled globally and per channel.
- Everything runs on every command, so checks must be memory-only.

## Decision

### 1. Roles are ranked integers

| Role | Rank | Source |
|------|------|--------|
| everyone | 0 | implicit |
| subscriber | 20 | badge |
| vip | 60 | badge |
| moderator | 80 | badge |
| lead_moderator | 90 | Badge when shown. Twitch lets lead mods display the plain mod badge, so it can also be assigned manually. |
| broadcaster | 100 | badge |
| custom (e.g. ambassador 50, trusted 85, co_streamer 95) | 1–99 | `role_members`, per channel or global |
| **bot_admin** (global) | 1000 | `global_admins`. Granted and revoked by a bot owner. Applies in every channel the bot is in. |
| **bot_owner** (global) | 10000 | config (`BOT_OWNER_IDS`). Can't be changed at runtime. |

- There is **no per-channel admin role.** "Admin" means the global bot owners and bot admins.
- **Effective rank** is the highest rank the user holds.
- A command passes if `rank ≥ required_role.rank`, **or** if the user holds one of the command's exact `allowed_roles`.
- **Grant rule:** you can only create, edit, grant or revoke roles ranked strictly below your own. The broadcaster can manage every custom role (1–99) in their channel. Only bot owners manage bot admins.
- **Custom role name collisions:** channel-level roles override global roles with the same name. Equal ranks break ties by role ID.

### 2. Two cooldowns, both required

- **Rule lookup:** the rule `(scope, command, role) → Cooldown(tier_s, user_s)` comes from the user's highest-ranked role that has a rule for this command. The lookup falls back to the command spec's defaults and then to the module's defaults.
  - *As implemented:* rules are merged in the order spec defaults → global → channel. The tier is the **highest-ranked rule role whose rank the user reaches** (rank ≤ the user's effective rank), the same ranking used for permissions. This way a broadcaster without a `moderator` badge still gets the moderator rule.
  - An implicit `moderator: 0/0` rule applies unless one is configured.
- **Tier bucket** `(channel, command, tier_role)`: the default meaning of "global". Each tier has its own shared timer.
- **User bucket** `(channel, command, user_id)`.
- A command runs only if **both** have expired. When it runs, **both** start.
- Triggers use `(channel, trigger_id)` as the tier bucket, plus the event user.
- Custom commands get their own buckets in addition to their inner commands' buckets (ADR-0009).
- Buckets live in memory on a monotonic clock and reset on restart.

### 3. Silent rejection plus callbacks

- Denials (code 126) and cooldowns (code 128) produce **no chat output** by default.
- Optional `on_cooldown` and `on_denied` callbacks, resolved in the order **command → module → channel**:
  - A Python hook `(CooldownInfo | DenialInfo, ctx) → Result | None`, **or**
  - An **expression** using `{cooldown.tier_remaining}`, `{cooldown.user_remaining}`, `{cooldown.tier}`, `{cooldown.command}`, `{denied.required_role}`, `{chatter.name}`.
- Callback output is rate-limited to one notice per `(channel, user, command)` per 30 s, and callbacks never trigger other callbacks.

### 4. Toggles resolve in layers

1. Global module kill-switch
2. Global command kill-switch
3. Channel command override
4. Channel module override
5. Global module default
6. Code default

- A module is additionally unavailable if its required **capabilities** (ADR-0007) are missing.
- `core_admin` can't be disabled.

### 5. Ignore list, audit log and in-memory policy

- **Ignore list:** global and per channel, keyed by `user_id`. The chat-bot badge is auto-ignored, and the bot always ignores itself. Ignored users are logged but never trigger anything.
- **Policy snapshot:** kept in memory and rebuilt on any write made through the repositories.
- **Audit log:** every write is recorded, whether it came from chat, the API or the web UI.

## Options Considered

| Option | Verdict |
|--------|---------|
| **Ranked roles + exact lists** (chosen) | Simple from chat ("VIP and up"). Custom tiers slot in anywhere, including above mod. |
| Fine-grained RBAC grants | Too heavy to manage from chat. Revisit when the web admin UI matures. |
| Badge-only tiers | Can't express custom roles. |

| Cooldown model | Verdict |
|----------------|---------|
| One global bucket for everyone | Mods and viewers block each other. Rejected. |
| **Tier bucket + user bucket, both required** (chosen) | Matches the requirement |
| Tier bucket that also blocks lower tiers | Kept as a possible per-command option |

| Rejection feedback | Verdict |
|--------------------|---------|
| Always reply | Spammy, and reveals which commands exist |
| **Silent + callbacks** (chosen) | Quiet by default, customizable, rate-limited |

## Consequences

- **Easier:** chat-based configuration, fast checks, accurate filtering in `!help` and `!explain`.
- **Harder:** roles that don't fit on a line need `allowed_roles`. Callback expressions are one more place that must avoid spam, handled by the rate limit.
- **Revisit:** fine-grained grants if the admin UI calls for them. Persisting cooldowns if restarts are frequent.

## Action Items

1. [x] Add migrations: `roles`, `role_members` (with expiry), `command_rules`, `cooldown_rules`, `module_toggles`, `command_toggles`, `callbacks`, `ignore_list`, `audit_log`. Global rows use `channel_id='*'`.
2. [x] Build `policy/`: `effective_rank`, cooldown resolution (dual buckets), `is_enabled` (layers + capabilities) and role management, with table-driven tests.
3. [x] Add the callback runner with rate limiting. *(30 s per channel/user/command/kind)*
4. [x] Add `core_admin` commands: `!role`, `!perm`, `!cooldown`, `!module`, `!cmd` (enable/disable/log), `!ignore`, `!prefix`, `!callback`, `!admin`.
5. [x] Move the cooldown check out of preflight so `||` can handle a cooldown failure (spec §5.2, now 1.1). *(2026-09-22: the executor looks before expanding arguments and claims — check and commit in one step — just before running, so a never-reached branch is never held to a cooldown and a repeat in one line meets the cooldown its first run started. `!explain` still reports cooldowns, without failing on them.)*
