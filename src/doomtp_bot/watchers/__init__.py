"""Chat watchers: in-order observers of every chat line that fire trigger events (ADR-0028)."""

from doomtp_bot.watchers.base import ChatWatcher, WatchEvent
from doomtp_bot.watchers.pyramid import PyramidWatcher

__all__ = ["ChatWatcher", "PyramidWatcher", "WatchEvent", "default_watchers"]


def default_watchers() -> list[ChatWatcher]:
    """The watchers the bot runs. Each one's event types are listed in `triggers.service.TRIGGER_TYPES`."""
    return [PyramidWatcher()]
