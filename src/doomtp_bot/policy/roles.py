"""Roles and ranks (ADR-0006 §1)."""

from __future__ import annotations

from dataclasses import dataclass

GLOBAL = "*"

BUILTIN_RANKS: dict[str, int] = {
    "everyone": 0,
    "subscriber": 20,
    "vip": 60,
    "moderator": 80,
    "lead_moderator": 90,
    "broadcaster": 100,
    "bot_admin": 1000,
    "bot_owner": 10000,
}
MODERATOR_RANK = BUILTIN_RANKS["moderator"]
BROADCASTER_RANK = BUILTIN_RANKS["broadcaster"]
BOT_ADMIN_RANK = BUILTIN_RANKS["bot_admin"]
BOT_OWNER_RANK = BUILTIN_RANKS["bot_owner"]
CUSTOM_RANK_MIN, CUSTOM_RANK_MAX = 1, 99

# Twitch badge set_id → built-in role. Founders are subscribers.
BADGE_ROLES: dict[str, str] = {
    "broadcaster": "broadcaster",
    "lead_moderator": "lead_moderator",
    "moderator": "moderator",
    "vip": "vip",
    "subscriber": "subscriber",
    "founder": "subscriber",
}


@dataclass(frozen=True, slots=True)
class Role:
    id: int
    channel_id: str  # GLOBAL for global roles
    name: str
    rank: int
    builtin: bool = False


def roles_from_badges(badges: frozenset[str] | set[str]) -> set[str]:
    return {BADGE_ROLES[b] for b in badges if b in BADGE_ROLES}


def can_manage_role(
    actor_rank: int, role_rank: int, *, actor_is_broadcaster: bool, role_is_channel: bool
) -> bool:
    """Grant rule: only roles ranked strictly below your own. The broadcaster manages all channel custom roles."""
    if actor_is_broadcaster and role_is_channel and CUSTOM_RANK_MIN <= role_rank <= CUSTOM_RANK_MAX:
        return True
    return role_rank < actor_rank
