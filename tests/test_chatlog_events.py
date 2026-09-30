"""The one reader (ADR-0024 §2): stored rows in EventSub's shape, golden-tested on lines logs.ivr.fi served."""

from __future__ import annotations

from typing import Any

import pytest

from doomtp_bot.chatlog import events
from doomtp_bot.history import irc_convert
from doomtp_bot.history.irc_parse import IrcLine, parse_line

# Real lines from logs.ivr.fi for #vexoulz, as `raw=true` returns them.
REPLY = (
    "@tmi-sent-ts=1767382193813;subscriber=1;id=90e878e1-6bdf-45c0-8cd6-18d123a304a1;room-id=38656648;"
    "user-id=38656648;display-name=vEXOULZ;badges=broadcaster/1,subscriber/3048,gold-pixel-heart/1;"
    "badge-info=subscriber/48;color=#FFC200;flags=;user-type=;emotes=emotesv2_ae2329347b8b40e5adc1d40a79414dfc:8-19;"
    "reply-parent-user-login=keeki_chan;reply-parent-user-id=90528258;"
    "reply-thread-parent-msg-id=28b6d957-a17c-4783-b8fe-e4da99743cec;reply-thread-parent-user-id=90528258;"
    "reply-thread-parent-display-name=ケーキちゃん;reply-parent-msg-id=28b6d957-a17c-4783-b8fe-e4da99743cec;"
    "reply-parent-display-name=ケーキちゃん;reply-parent-msg-body=https://www.youtube.com/shorts/PURhWbQOjew;"
    "reply-thread-parent-user-login=keeki_chan :vexoulz!vexoulz@vexoulz.tmi.twitch.tv PRIVMSG #vexoulz "
    ":@ケーキちゃん vexoulSEETHE"
)
RAID = (
    "@tmi-sent-ts=1769898293340;id=22048eb4-0468-4816-a011-d092866c2a7c;room-id=38656648;user-id=263489526;"
    "login=cyaniderx;display-name=CyanideRx;badges=mel/1;badge-info=;color=#FF004D;flags=;user-type=;emotes=;"
    "msg-param-viewerCount=8;msg-param-login=cyaniderx;msg-id=raid;"
    "msg-param-profileImageURL=https://static-cdn.jtvnw.net/jtv_user_pictures/x-profile_image-%s.png;"
    "system-msg=8\\sraiders\\sfrom\\sCyanideRx\\shave\\sjoined!;msg-param-displayName=CyanideRx "
    ":tmi.twitch.tv USERNOTICE #vexoulz"
)
SUBGIFT = (
    "@tmi-sent-ts=1767904101197;subscriber=1;mod=1;id=7b1b7b6a-08af-4776-bc07-15593703d5f3;room-id=38656648;"
    "user-id=90528258;login=keeki_chan;display-name=ケーキちゃん;badges=lead_moderator/1,subscriber/3030,bits/5000;"
    "badge-info=subscriber/30;color=#F58C36;flags=;user-type=mod;emotes=;msg-param-recipient-display-name=bwead___;"
    "msg-param-origin-id=10088573622208416343;msg-id=subgift;msg-param-sub-plan-name=uhmm;msg-param-sub-plan=1000;"
    "msg-param-months=20;msg-param-recipient-id=241402398;msg-param-recipient-user-name=bwead___;"
    "msg-param-sender-count=319;msg-param-gift-months=1;system-msg=ケーキちゃん\\sgifted\\sa\\sTier\\s1\\ssub\\sto"
    "\\sbwead___! :tmi.twitch.tv USERNOTICE #vexoulz"
)
RESUB = (
    "@tmi-sent-ts=1769288340947;subscriber=1;mod=1;id=d225568a-9e28-4c09-91d9-fa5e3ea47cc9;room-id=38656648;"
    "user-id=38877316;login=annika_lily;display-name=annika_lily;badges=moderator/1,subscriber/24,bits-charity/1;"
    "badge-info=subscriber/28;color=#FF8EC7;flags=;user-type=mod;emotes=;msg-param-cumulative-months=28;"
    "msg-param-months=0;msg-param-sub-plan=1000;msg-param-was-gifted=false;msg-param-multimonth-tenure=23;"
    "msg-param-sub-plan-name=uhmm;msg-id=resub;msg-param-should-share-streak=0;msg-param-multimonth-duration=24;"
    "system-msg=annika_lily\\ssubscribed\\sat\\sTier\\s1. :tmi.twitch.tv USERNOTICE #vexoulz :my streamer Stronge"
)
CHARITY = (
    "@tmi-sent-ts=1770482190727;subscriber=1;mod=1;id=7265aa5f-2185-4b80-a99d-ce04f9aaf1ba;room-id=38656648;"
    "user-id=1195395;login=tommyboy9000;display-name=tommyboy9000;badges=moderator/1;badge-info=;color=#9ACD32;"
    "flags=;user-type=mod;emotes=;msg-param-donation-currency=USD;msg-param-donation-amount=1500;"
    "system-msg=tommyboy9000:\\sDonated\\sUSD\\s15;msg-param-charity-name=Global\\sAction\\sfor\\sTrans\\sEquality;"
    "msg-param-exponent=2;msg-id=charitydonation :tmi.twitch.tv USERNOTICE #vexoulz"
)
MYSTERY = (
    "@tmi-sent-ts=1770487434447;id=23a20d1b-5516-477e-bf56-a68b194cd25f;room-id=38656648;user-id=1195395;"
    "login=tommyboy9000;display-name=tommyboy9000;badges=;badge-info=;color=#9ACD32;flags=;user-type=mod;emotes=;"
    "msg-param-community-gift-id=1708046750914149764;msg-param-mass-gift-count=1;msg-param-sender-count=22;"
    "system-msg=tommyboy9000\\sis\\sgifting\\s1\\sTier\\s1\\sSubs;msg-id=submysterygift;"
    "msg-param-origin-id=1708046750914149764;msg-param-sub-plan=1000 :tmi.twitch.tv USERNOTICE #vexoulz"
)
MILESTONE = (
    "@tmi-sent-ts=1769881049171;id=437a4076-156f-4cbf-8fc9-22f74cb83395;room-id=38656648;user-id=90528258;"
    "login=keeki_chan;display-name=ケーキちゃん;badges=;badge-info=;color=#F58C36;flags=;user-type=mod;emotes=;"
    "msg-param-value=10;msg-param-copoReward=450;msg-id=viewermilestone;msg-param-category=watch-streak;"
    "msg-param-id=6646b7c1-0e98-4228-afba-b8f1a0fa8b7a;system-msg=ケーキちゃん\\swatched\\s10\\sconsecutive\\sstreams "
    ":tmi.twitch.tv USERNOTICE #vexoulz :lag"
)
EMOTE = "emotesv2_ae2329347b8b40e5adc1d40a79414dfc"
NO_ONE = {"cheermote": None, "emote": None, "mention": None}


def line_of(raw: str) -> IrcLine:
    line = parse_line(raw)
    assert line is not None
    return line


def notice(raw: str) -> dict[str, Any]:
    return events.notification("irc", {"line": raw})


# ── messages ───────────────────────────────────────────────────────────────
def test_a_reply_with_an_emote_is_a_chat_message_event() -> None:
    assert events.message("irc", {"line": REPLY}) == {
        "broadcaster_user_id": "38656648",
        "broadcaster_user_login": "vexoulz",
        "broadcaster_user_name": None,
        "chatter_user_id": "38656648",
        "chatter_user_login": "vexoulz",
        "chatter_user_name": "vEXOULZ",
        "message_id": "90e878e1-6bdf-45c0-8cd6-18d123a304a1",
        "message": {
            "text": "@ケーキちゃん vexoulSEETHE",
            "fragments": [
                {"type": "text", "text": "@ケーキちゃん ", **NO_ONE},
                {"type": "emote", "text": "vexoulSEETHE", **NO_ONE, "emote": {"id": EMOTE}},
            ],
        },
        "color": "#FFC200",
        "badges": [
            {"set_id": "broadcaster", "id": "1", "info": ""},
            {"set_id": "subscriber", "id": "3048", "info": "48"},
            {"set_id": "gold-pixel-heart", "id": "1", "info": ""},
        ],
        "message_type": "text",
        "cheer": None,
        "reply": {
            "parent_message_id": "28b6d957-a17c-4783-b8fe-e4da99743cec",
            "parent_user_id": "90528258",
            "parent_user_login": "keeki_chan",
            "parent_user_name": "ケーキちゃん",
            "parent_message_body": "https://www.youtube.com/shorts/PURhWbQOjew",
            "thread_message_id": "28b6d957-a17c-4783-b8fe-e4da99743cec",
            "thread_user_id": "90528258",
            "thread_user_login": "keeki_chan",
            "thread_user_name": "ケーキちゃん",
        },
        "channel_points_custom_reward_id": None,
        "source_broadcaster_user_id": None,
        "source_message_id": None,
        "source_badges": None,
        "irc": {"flags": "", "subscriber": "1", "user-type": ""},
    }


def test_a_bits_message_has_a_cheer() -> None:
    line = "@id=m1;user-id=400;bits=100;tmi-sent-ts=1 :a!a@a PRIVMSG #doomtp :Cheer100 nice"
    assert events.message("irc", {"line": line})["cheer"] == {"bits": 100}


def test_eventsub_and_legacy_rows_are_read_as_stored() -> None:
    raw = {"message_id": "m1", "message": {"text": "hi", "fragments": []}}
    assert events.message("eventsub", raw) == raw
    assert events.message("legacy", raw) == raw
    assert events.message(None, None) == {}


def test_an_irc_row_whose_line_does_not_parse_is_an_error() -> None:
    with pytest.raises(ValueError):
        events.message("irc", {"line": ""})


# ── notifications ──────────────────────────────────────────────────────────
def test_a_raid_names_the_raider_as_eventsub_does() -> None:
    event = notice(RAID)
    assert event["notice_type"] == "raid"
    assert event["system_message"] == "8 raiders from CyanideRx have joined!"
    assert event["message"] == {"text": "", "fragments": []}
    assert event["raid"] == {
        "user_id": "263489526",
        "user_login": "cyaniderx",
        "user_name": "CyanideRx",
        "viewer_count": 8,
        "profile_image_url": "https://static-cdn.jtvnw.net/jtv_user_pictures/x-profile_image-%s.png",
    }
    assert event["irc"] == {"flags": "", "user-type": ""}


def test_a_sub_gift_keeps_what_eventsub_lacks_under_its_irc_names() -> None:
    event = notice(SUBGIFT)
    assert event["notice_type"] == "sub_gift"
    assert event["chatter_user_name"] == "ケーキちゃん"
    assert event["sub_gift"] == {
        "duration_months": 1,
        "cumulative_total": 319,
        "recipient_user_id": "241402398",
        "recipient_user_login": "bwead___",
        "recipient_user_name": "bwead___",
        "sub_tier": "1000",
        "community_gift_id": None,
    }
    assert event["irc"] == {
        "subscriber": "1",
        "mod": "1",
        "flags": "",
        "user-type": "mod",
        "msg-param-origin-id": "10088573622208416343",
        "msg-param-sub-plan-name": "uhmm",
        "msg-param-months": "20",
    }


def test_a_resub_hides_a_streak_the_chatter_did_not_share() -> None:
    event = notice(RESUB)
    assert event["message"]["text"] == "my streamer Stronge"
    assert event["resub"] == {
        "cumulative_months": 28,
        "duration_months": 24,
        "streak_months": None,
        "sub_tier": "1000",
        "is_prime": False,
        "is_gift": False,
        "gifter_is_anonymous": None,
        "gifter_user_id": None,
        "gifter_user_login": None,
        "gifter_user_name": None,
    }


def test_a_charity_donation_has_an_amount() -> None:
    assert notice(CHARITY)["charity_donation"] == {
        "charity_name": "Global Action for Trans Equality",
        "amount": {"value": 1500, "decimal_places": 2, "currency": "USD"},
    }


def test_a_mystery_gift_is_a_community_sub_gift() -> None:
    event = notice(MYSTERY)
    assert event["notice_type"] == "community_sub_gift"
    assert event["community_sub_gift"] == {
        "id": "1708046750914149764",
        "total": 1,
        "sub_tier": "1000",
        "cumulative_total": 22,
    }


def test_a_notice_eventsub_has_no_object_for_keeps_every_param() -> None:
    event = notice(MILESTONE)
    assert event["notice_type"] == "watch_streak"
    assert event["watch_streak"] == {
        "value": "10",
        "copo_reward": "450",
        "category": "watch-streak",
        "id": "6646b7c1-0e98-4228-afba-b8f1a0fa8b7a",
    }


def test_an_anonymous_gift_names_no_one() -> None:
    line = (
        "@id=n1;user-id=274598607;login=ananonymousgifter;msg-id=subgift;msg-param-recipient-id=5;"
        "msg-param-sub-plan=1000;tmi-sent-ts=1 :tmi.twitch.tv USERNOTICE #doomtp"
    )
    event = notice(line)
    assert event["chatter_is_anonymous"] is True
    assert event["chatter_user_id"] is None


# ── moderation ─────────────────────────────────────────────────────────────
def test_clearmsg_is_a_message_delete() -> None:
    line = "@login=alice;room-id=1;target-msg-id=m1;tmi-sent-ts=1 :tmi.twitch.tv CLEARMSG #doomtp :bad words"
    assert events.moderation("irc", {"line": line}) == {
        "broadcaster_user_id": "1",
        "broadcaster_user_login": "doomtp",
        "broadcaster_user_name": None,
        "target_user_id": None,
        "target_user_login": "alice",
        "target_user_name": None,
        "message_id": "m1",
        "irc": {"text": "bad words"},
    }


def test_a_timeout_keeps_its_length_under_irc() -> None:
    line = (
        "@ban-duration=600;room-id=1;target-user-id=400;tmi-sent-ts=1 :tmi.twitch.tv CLEARCHAT #doomtp :alice"
    )
    event = events.moderation("irc", {"line": line})
    assert event["target_user_id"] == "400"
    assert event["target_user_login"] == "alice"
    assert event["irc"] == {"ban-duration": "600"}


def test_clearchat_without_a_target_is_a_chat_clear() -> None:
    line = "@room-id=1;tmi-sent-ts=1 :tmi.twitch.tv CLEARCHAT #doomtp"
    assert events.moderation("irc", {"line": line}) == {
        "broadcaster_user_id": "1",
        "broadcaster_user_login": "doomtp",
        "broadcaster_user_name": None,
        "irc": {},
    }


# ── enrichment ─────────────────────────────────────────────────────────────
ENRICHMENT = {
    "emotes": {EMOTE: {"emote_set_id": "300", "owner_id": "38656648", "format": ["static"], "source": "helix"}},
    "mentions": {
        "ケーキちゃん": {"user_id": "90528258", "user_login": "keeki_chan", "user_name": "ケーキちゃん",
                         "source": "reply"},
    },
}  # fmt: skip


def test_enrichment_adds_emote_details_and_splits_out_mentions() -> None:
    fragments = events.message("irc", {"line": REPLY}, ENRICHMENT)["message"]["fragments"]
    assert fragments == [
        {"type": "mention", "text": "@ケーキちゃん", **NO_ONE, "mention": ENRICHMENT["mentions"]["ケーキちゃん"]},
        {"type": "text", "text": " ", **NO_ONE},
        {"type": "emote", "text": "vexoulSEETHE", **NO_ONE,
         "emote": {"id": EMOTE, **ENRICHMENT["emotes"][EMOTE]}},
    ]  # fmt: skip


def test_a_mention_keeps_its_trailing_punctuation_as_text() -> None:
    mention = {"user_id": "5", "user_login": "bob", "user_name": "Bob", "source": "log"}
    fragments = events.apply(
        [{"type": "text", "text": "hi @Bob, ok", **NO_ONE}], {"mentions": {"bob": mention}}
    )
    assert [(f["type"], f["text"]) for f in fragments] == [
        ("text", "hi "),
        ("mention", "@Bob"),
        ("text", ", ok"),
    ]


def test_a_cheermote_is_split_out_whatever_its_case() -> None:
    cheer = {"prefix": "cheer", "bits": 100, "tier": 100, "source": "twitch"}
    fragments = events.apply(
        [{"type": "text", "text": "Cheer100 nice", **NO_ONE}], {"cheermotes": {"cheer100": cheer}}
    )
    assert fragments == [
        {"type": "cheermote", "text": "Cheer100", **NO_ONE, "cheermote": cheer},
        {"type": "text", "text": " nice", **NO_ONE},
    ]


def test_an_unknown_mention_stays_text() -> None:
    text = [{"type": "text", "text": "hi @nobody", **NO_ONE}]
    assert events.apply(text, {"mentions": {"bob": {}}}) == text


# ── the /log fragment ──────────────────────────────────────────────────────
def test_entry_fragments_keep_the_log_shape_and_add_what_was_found() -> None:
    fragments = events.message("irc", {"line": REPLY}, ENRICHMENT)["message"]["fragments"]
    assert [events.entry_fragment(f) for f in fragments] == [
        {"type": "mention", "text": "@ケーキちゃん",
         "mention": {"id": "90528258", "login": "keeki_chan", "user_name": "ケーキちゃん", "source": "reply"}},
        {"type": "text", "text": " "},
        {"type": "emote", "text": "vexoulSEETHE", "emote_id": EMOTE,
         "emote": {"emote_set_id": "300", "owner_id": "38656648", "format": ["static"], "source": "helix"}},
    ]  # fmt: skip


def test_an_unenriched_emote_is_only_its_id() -> None:
    fragments = events.message("irc", {"line": REPLY})["message"]["fragments"]
    assert events.entry_fragment(fragments[1]) == {"type": "emote", "text": "vexoulSEETHE", "emote_id": EMOTE}


def test_every_real_line_converts() -> None:
    """A smoke check that the converters take every kind of line the golden ones came from."""
    for raw in (REPLY, RAID, SUBGIFT, RESUB, CHARITY, MYSTERY, MILESTONE):
        line = line_of(raw)
        if line.command == "PRIVMSG":
            assert irc_convert.message_event(line)["message_id"]
        else:
            assert irc_convert.notification_event(line)["notice_type"]
