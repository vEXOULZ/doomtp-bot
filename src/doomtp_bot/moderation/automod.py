"""AutoMod-assisted filtering: acting on incoming chat, not just on what the bot says (§9.3).

The channel's badword list already censors every outgoing message. This is the optional other half:
when a chatter says something the list would *block*, the bot deletes it, and times the chatter out if
the channel asked for that.

Three things keep it from surprising anyone:
  * it is off until a moderator turns it on, per channel;
  * it needs the moderator tier — without `moderate` the bot cannot delete anything anyway (ADR-0007);
  * only `block` entries count. `mask`, `replace` and `tag` rewrite what the *bot* says; they are not a
    judgement about the chatter, so they never cost anyone a message.

Moderators and the broadcaster are never actioned: a mod quoting a slur to talk about it is a normal
part of moderating, and a bot that deletes its own moderators is worse than no bot.

Twitch echoes the delete back as a `channel.chat.message_delete` event, so the chat log and the
moderation index record it the same way they record a human mod's delete. Nothing is logged twice here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import structlog

from doomtp_bot.core.capabilities import MODERATE
from doomtp_bot.policy.roles import MODERATOR_RANK

if TYPE_CHECKING:
    from doomtp_bot.core.events import ChatMessage
    from doomtp_bot.filters.service import FilterService
    from doomtp_bot.policy.service import PolicyService

log = structlog.get_logger(__name__)

ACTIONS = ("off", "delete", "timeout")
MAX_TIMEOUT_S = 1_209_600  # Twitch's ceiling: 14 days
REASON = "filtered by the channel's word list"


@dataclass(frozen=True, slots=True)
class Verdict:
    """What the bot is about to do, and why. Built without any I/O so the hot path stays cheap."""

    action: str  # "delete" | "timeout"
    patterns: tuple[str, ...]
    seconds: int = 0


class Moderator(Protocol):
    """The moderation calls AutoMod needs. TwitchService implements it; tests fake it."""

    async def delete_message(self, channel_id: str, message_id: str) -> bool: ...

    async def timeout_user(self, channel_id: str, user_id: str, seconds: int, reason: str) -> bool: ...


class AutoMod:
    def __init__(self, *, policy: PolicyService, filters: FilterService, moderator: Moderator) -> None:
        self.policy = policy
        self.filters = filters
        self.moderator = moderator

    def verdict(self, msg: ChatMessage) -> Verdict | None:
        """The whole decision, synchronously: settings, capability, exemption, then the filter."""
        settings = self.policy.channel_settings(msg.channel_id)
        if settings is None or settings.automod_action == "off":
            return None
        if MODERATE not in settings.capabilities:
            return None  # the bot isn't a moderator here; it could not delete anything anyway
        chatter = self.policy.build_chatter(
            msg.channel_id,
            msg.user_id,
            msg.user_login,
            msg.display_name,
            frozenset(b.set_id for b in msg.badges),
        )
        if chatter.rank >= MODERATOR_RANK:
            return None
        result = self.filters.check(msg.channel_id, msg.text)
        if not result.blocked:
            return None
        patterns = tuple(result.patterns())
        if settings.automod_action == "timeout":
            return Verdict("timeout", patterns, min(max(settings.automod_timeout_s, 1), MAX_TIMEOUT_S))
        return Verdict("delete", patterns)

    async def enforce(self, msg: ChatMessage, verdict: Verdict) -> None:
        """Delete, then time out if that is the setting. A failed call is logged, never raised."""
        deleted = await self.moderator.delete_message(msg.channel_id, msg.message_id)
        timed_out = False
        if verdict.action == "timeout":
            timed_out = await self.moderator.timeout_user(
                msg.channel_id, msg.user_id, verdict.seconds, REASON
            )
        log.info(
            "automod.acted",
            channel=msg.channel_id,
            user=msg.user_id,
            message_id=msg.message_id,
            patterns=list(verdict.patterns),
            deleted=deleted,
            timed_out=timed_out,
            seconds=verdict.seconds,
        )
