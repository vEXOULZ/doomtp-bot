"""Recorded EventSub notifications, replayed through TwitchIO's own parser (ADR-0002 item 4).

`test_twitch.py` maps hand-built payload objects, which proves the adapter's logic but not that Twitch
still sends what TwitchIO expects. These fixtures come from Twitch's simulator — `scripts/record_eventsub.py`
writes them — and go through the same three steps as a live event: envelope → TwitchIO model → domain
event. A shape change on Twitch's side, or a rename inside TwitchIO, fails here instead of in production.

The CLI cannot trigger `channel.chat.*`, so chat messages and notices are not covered; those stay on fakes.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from twitchio import eventsub
from twitchio.eventsub.subscriptions import _SUB_MAPPING
from twitchio.models.eventsub_ import create_event_instance

from doomtp_bot.core.events import ChatNotification
from doomtp_bot.twitch import mapping
from doomtp_bot.twitch.client import _BotClient

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "eventsub"
#: What the bot subscribes to, and the handler TwitchIO is expected to call for it.
SUBSCRIPTIONS = (
    (eventsub.ChannelFollowSubscription, "event_follow"),
    (eventsub.ChannelPointsRedeemAddSubscription, "event_custom_redemption_add"),
    (eventsub.ChannelCheerSubscription, "event_cheer"),
)


def envelope(subscription_type: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{subscription_type}.json").read_text(encoding="utf-8"))


def parsed(subscription_type: str, raw: dict[str, Any] | None = None) -> Any:
    """The TwitchIO model object a live notification would produce, without a live connection."""
    raw = raw if raw is not None else envelope(subscription_type)
    return create_event_instance(subscription_type, raw, http=None)


@pytest.mark.parametrize(("subscription", "handler"), SUBSCRIPTIONS, ids=lambda v: getattr(v, "type", v))
def test_the_recorded_version_is_the_one_the_bot_subscribes_to(subscription: Any, handler: str) -> None:
    recorded = envelope(subscription.type)["metadata"]

    assert recorded["subscription_version"] == subscription.version
    # TwitchIO turns the subscription type into the handler name it dispatches to; ours must match.
    derived = _SUB_MAPPING.get(subscription.type, subscription.type.removeprefix("channel.")).replace(
        ".", "_"
    )
    assert f"event_{derived}" == handler
    assert callable(getattr(_BotClient, handler))


def test_a_recorded_follow_becomes_a_follow_notification() -> None:
    event = mapping.follow(parsed("channel.follow"))

    assert isinstance(event, ChatNotification)
    assert (event.type, event.channel_id, event.user_id) == ("follow", "40174384", "80730642")
    assert event.payload["system_message"] == "testFromUser followed"
    assert event.sent_at == event.payload["followed_at"] == 1767268800000  # the recorded followed_at
    assert event.id == "follow:80730642:1767268800000"  # Twitch sends no id for follows, so we build one


def test_a_recorded_redemption_carries_the_reward_and_the_input() -> None:
    event = mapping.redemption(parsed("channel.channel_points_custom_reward_redemption.add"))

    assert (event.type, event.channel_id, event.user_id) == ("redemption", "40174384", "80730642")
    assert event.id == "redemption:5fc4e4ed-5e1b-4dd2-9a0d-0de0ad9c9a19"  # dedupes on Twitch's own id
    assert event.payload["reward"] == {
        "id": "92af127c-7326-4483-a52b-b0da0be61c01",
        "title": "Hydrate",
        "cost": 500,
    }
    # {event.input} is what the viewer typed into the reward (architecture §7).
    assert event.payload["input"] == event.payload["text"] == "Test Input From CLI"
    assert event.payload["status"] == "unfulfilled"


def test_a_recorded_cheer_carries_the_bits_and_the_message() -> None:
    event = mapping.cheer(parsed("channel.cheer"))

    assert (event.type, event.channel_id, event.user_id) == ("cheer", "40174384", "80730642")
    assert event.payload["bits"] == 250 and not event.payload["anonymous"]
    assert event.payload["text"] == "This is a test event."
    assert event.payload["system_message"] == "testFromUser cheered 250 bits"
    # ChannelCheer has no timestamp of its own; TwitchIO takes it from the message metadata.
    assert event.sent_at == 1767268800000


def test_an_anonymous_cheer_names_nobody() -> None:
    # The simulator only sends identified cheers (`--anonymous` covers gifts and subs), so this is the
    # recorded payload with the fields Twitch nulls out for an anonymous cheer.
    raw = deepcopy(envelope("channel.cheer"))
    raw["payload"]["event"].update(
        is_anonymous=True, user_id=None, user_login=None, user_name=None, message="have some bits"
    )

    event = mapping.cheer(parsed("channel.cheer", raw))

    assert event.user_id is None and event.payload["user"] is None
    assert event.payload["anonymous"] and event.payload["system_message"] == "someone cheered 250 bits"
