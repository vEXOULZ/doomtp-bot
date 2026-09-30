"""IRC lines in EventSub's terms (ADR-0024 §2): fragments, message and notice types, notice payloads."""

from __future__ import annotations

import pytest

from doomtp_bot.core.events import Badge, ChatMessage, ChatNotification, MessageDeleted
from doomtp_bot.core.events import UserMessagesCleared as UserCleared
from doomtp_bot.history import irc_convert
from doomtp_bot.history.backfill import to_events
from doomtp_bot.history.irc_parse import IrcLine, parse_line


def line_of(raw: str) -> IrcLine:
    line = parse_line(raw)
    assert line is not None
    return line


def privmsg(text: str, tags: str = "") -> IrcLine:
    return line_of(
        f"@id=m1;user-id=400;tmi-sent-ts=1000{';' + tags if tags else ''} :alice!alice@x PRIVMSG #doomtp :{text}"
    )


# ── fragments ──────────────────────────────────────────────────────────────
def test_a_line_without_emotes_is_one_text_fragment() -> None:
    assert irc_convert.fragments(privmsg("hello there")) == ({"type": "text", "text": "hello there"},)


def test_emotes_split_the_text_where_the_tag_says() -> None:
    line = privmsg("Kappa hi Kappa PogChamp", "emotes=25:0-4,9-13/88:15-22")
    assert irc_convert.fragments(line) == (
        {"type": "emote", "text": "Kappa", "emote_id": "25"},
        {"type": "text", "text": " hi "},
        {"type": "emote", "text": "Kappa", "emote_id": "25"},
        {"type": "text", "text": " "},
        {"type": "emote", "text": "PogChamp", "emote_id": "88"},
    )


def test_emote_positions_count_code_points() -> None:
    line = privmsg("😀 Kappa", "emotes=25:2-6")
    assert irc_convert.fragments(line)[-1] == {"type": "emote", "text": "Kappa", "emote_id": "25"}


def test_an_emote_tag_that_does_not_fit_the_text_is_left_as_text() -> None:
    line = privmsg("hi", "emotes=25:0-40")
    assert irc_convert.fragments(line) == ({"type": "text", "text": "hi"},)


def test_a_me_message_loses_its_action_wrapper() -> None:
    line = privmsg("\x01ACTION waves Kappa\x01", "emotes=25:6-10")
    assert irc_convert.text(line) == "waves Kappa"
    assert irc_convert.fragments(line)[-1] == {"type": "emote", "text": "Kappa", "emote_id": "25"}


# ── messages ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("msg_id", "expected"),
    [
        ("", "text"),
        ("highlighted-message", "channel_points_highlighted"),
        ("skip-subs-mode-message", "channel_points_sub_only"),
        ("user-intro", "user_intro"),
        ("gigantified-emote-message", "power_ups_gigantified_emote"),
        ("animated-message", "power_ups_message_effect"),
    ],
)
def test_msg_id_is_the_message_type(msg_id: str, expected: str) -> None:
    assert irc_convert.message_type(privmsg("hi", f"msg-id={msg_id}" if msg_id else "")) == expected


def test_a_message_keeps_what_eventsub_would_have_sent() -> None:
    raw = (
        "@badge-info=subscriber/14;badges=subscriber/12,moderator/1;id=m1;user-id=400;tmi-sent-ts=1000;"
        "custom-reward-id=r-1;source-room-id=200;reply-parent-msg-id=p-1;reply-parent-user-id=500;"
        "reply-parent-user-login=bob;reply-parent-display-name=Bob;emotes=25:5-9"
        " :alice!alice@x PRIVMSG #doomtp :@bob Kappa"
    )
    event = to_events(line_of(raw), "100", "doomtp", raw)
    assert isinstance(event, ChatMessage)
    assert event.badges == (Badge("subscriber", "12", "14"), Badge("moderator", "1", ""))
    assert event.fragments[-1] == {"type": "emote", "text": "Kappa", "emote_id": "25"}
    assert (event.reward_id, event.source_channel_id) == ("r-1", "200")
    assert (event.reply_parent_id, event.reply_parent_user_id) == ("p-1", "500")


# ── notices ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ("msg-id=resub", "resub"),
        ("msg-id=subgift", "sub_gift"),
        ("msg-id=submysterygift", "community_sub_gift"),
        ("msg-id=viewermilestone", "watch_streak"),
        ("msg-id=ritual", "ritual"),  # nothing in EventSub for it: keeps its IRC name
        ("msg-id=sharedchatnotice;source-msg-id=subgift", "shared_chat_sub_gift"),
    ],
)
def test_msg_id_is_the_notice_type(tags: str, expected: str) -> None:
    line = line_of(f"@{tags};id=n1;user-id=400;tmi-sent-ts=1 :tmi.twitch.tv USERNOTICE #doomtp")
    assert irc_convert.notice_type(line) == expected


def test_a_notice_payload_has_the_live_shape() -> None:
    raw = (
        "@msg-id=resub;msg-param-cumulative-months=12;msg-param-sub-plan=1000;login=alice;"
        "system-msg=Alice\\ssubscribed;id=n1;user-id=400;tmi-sent-ts=5000"
        " :tmi.twitch.tv USERNOTICE #doomtp :thanks!"
    )
    event = to_events(line_of(raw), "100", "doomtp", raw)
    assert isinstance(event, ChatNotification)
    assert (event.type, event.user_id) == ("resub", "400")
    assert event.payload == {
        "system_message": "Alice subscribed",
        "text": "thanks!",
        "chatter": {"id": "400", "login": "alice"},
        "detail": {"cumulative_months": "12", "sub_plan": "1000"},
    }


def test_an_anonymous_gift_names_no_one() -> None:
    raw = (
        "@msg-id=submysterygift;login=ananonymousgifter;user-id=274598607;id=n2;tmi-sent-ts=1"
        " :tmi.twitch.tv USERNOTICE #doomtp"
    )
    event = to_events(line_of(raw), "100", "doomtp", raw)
    assert isinstance(event, ChatNotification)
    assert (event.type, event.user_id, event.payload["chatter"]) == ("community_sub_gift", None, None)


# ── moderation ─────────────────────────────────────────────────────────────
def test_a_timeout_keeps_its_length_and_a_ban_has_none() -> None:
    timeout = "@ban-duration=600;target-user-id=400;tmi-sent-ts=3000 :tmi.twitch.tv CLEARCHAT #doomtp :alice"
    ban = "@target-user-id=400;tmi-sent-ts=3000 :tmi.twitch.tv CLEARCHAT #doomtp :alice"
    first, second = (to_events(line_of(raw), "100", "doomtp", raw) for raw in (timeout, ban))
    assert isinstance(first, UserCleared) and first.duration_s == 600
    assert isinstance(second, UserCleared) and second.duration_s is None


def test_a_delete_does_not_take_the_login_for_a_user_id() -> None:
    raw = "@login=alice;target-msg-id=m1;tmi-sent-ts=2000 :tmi.twitch.tv CLEARMSG #doomtp :hi"
    event = to_events(line_of(raw), "100", "doomtp", raw)
    assert isinstance(event, MessageDeleted) and event.target_user_id is None


def test_camel_case_params_are_snake_case_as_in_eventsub() -> None:
    raw = (
        "@msg-id=raid;msg-param-viewerCount=8;msg-param-profileImageURL=x;msg-param-login=cyan;"
        "id=n3;user-id=263;tmi-sent-ts=1 :tmi.twitch.tv USERNOTICE #doomtp"
    )
    assert irc_convert.notice_payload(line_of(raw))["detail"] == {
        "viewer_count": "8",
        "profile_image_url": "x",
        "login": "cyan",
    }
