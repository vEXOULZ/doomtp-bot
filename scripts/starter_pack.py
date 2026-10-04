"""The derived commands the bot ships: the `core` system pack and the `starter` set (ADR-0012 item 6,
ADR-0019).

A derived command is an ordinary custom command, so these are written in the command language rather than
Python: readable with `!cc info`, versioned, fixable live. They belong to the bot's own account and go
out as packs:

- `core` is a **system pack**: sentinels like `false` and `default`, which resolve everywhere without being
  published and can't be switched off. The bot refuses to start without it, so run this script before the
  first start and after every upgrade.
- `starter` is published globally, and a channel switches it off like anything else
  (`!module disable starter`, or `!cmd disable hug`).

    python scripts/starter_pack.py --database-url postgresql://doomtp@localhost/doomtp
    docker compose --profile tools run --rm starter-pack

Running it again edits what changed here and leaves the rest alone, which is how an upgrade ships a fix.
The starter pack is safe to update while the bot is running: resolution reads publications from the
database every time, and an edit bumps the version the parsed-body cache is keyed by. The bot loads
`core` once at startup, so restart it after a `core` change.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

import psycopg

from doomtp_bot.clock import now_ms
from doomtp_bot.config import Settings
from doomtp_bot.customcmds.packs import RESERVED_PACK_NAMES, Pack, PackService
from doomtp_bot.customcmds.service import CustomCommandError, CustomCommandService
from doomtp_bot.customcmds.system import (
    CORE,
    CORE_COMMANDS,
    CORE_SUMMARY,
    CORE_VERSION,
    Derived,
    NotASentinel,
    check_sentinel_body,
)
from doomtp_bot.filters.service import FilterService
from doomtp_bot.lang.parser import DEFAULT_PREFIX
from doomtp_bot.modules import builtin_registry
from doomtp_bot.policy.roles import GLOBAL
from doomtp_bot.storage.db import Connection, check_schema, configure_event_loop, connect, fetch_value
from doomtp_bot.storage.schema import SchemaMismatch

PACK = "starter"
PACK_SUMMARY = "The commands every channel starts with"


def echo_or_custom(name: str, wording: str) -> str:
    """A readout's `echo`: the channel's own wording from `!customecho set <name> …` if it has one,
    else `wording` (ADR-0019)."""
    return f"echo {{channel.customecho[{name}]:template ?? {wording}}}"


STARTER: tuple[Derived, ...] = (
    Derived(name="ping", summary="Check that the bot is alive", body="echo pong"),
    Derived(
        name="hug",
        summary="Hug someone, or the whole chat",
        body="echo {$chatter.display} hugs {arg.1 ?? the whole chat} 🫂",
        declarations=('1 name=target type=user required=no "who to hug"',),
    ),
    Derived(
        name="lurk",
        summary="Say you're still around, quietly",
        body="echo thanks for the lurk, {$chatter.display}, your seat stays warm",
    ),
    Derived(
        name="roll",
        summary="Roll a die",
        body="random {arg.1 ?? 1-20} | echo {$chatter.display} rolled {_1}",
        declarations=('1 name=range type=range required=no "a range like 1-6 (default 1-20)"',),
    ),
    Derived(
        name="so",
        summary="Shout out another streamer",
        # A moderator's `so` also sends Twitch's shoutout card, when the bot is a moderator and the stream
        # is live; for anyone else, or when the card can't go out, it is just the line.
        body=(
            "ifelse {$chatter.is_mod} ( shoutout {arg.streamer[name]} || true )"
            " && echo go follow twitch.tv/{arg.streamer[name]}. They were last seen being excellent"
        ),
        declarations=('1 name=streamer type=user "whose channel to name"',),
        note="anyone can run it; only a moderator's also sends Twitch's shoutout card",
    ),
    Derived(
        name="deaths",
        summary="Count the deaths of the run",
        body="var incr channel.deaths | echo deaths: {_1}",
        note="writes a channel variable, so each channel allows it once: `!cc grant deaths channel.deaths`",
    ),
    # Readouts: what the channel and the bot look like right now, each rewordable per channel.
    Derived(
        name="uptime",
        summary="How long the stream has been live",
        body=(
            "ifelse {$channel.live} ( "
            + echo_or_custom("uptime", "{$channel.display} has been live for {$channel.uptime:human ?? a moment}")
            + " ) ( echo {$channel.display} isn't live right now )"
        ),
    ),
    Derived(
        name="title",
        summary="The stream's title",
        body=echo_or_custom("title", "{$channel.title ?? no title, the stream is offline}"),
    ),
    Derived(
        name="game",
        summary="What the stream is playing",
        body=echo_or_custom("game", "{$channel.display} is playing {$channel.game ?? nothing right now}"),
    ),
    Derived(
        name="viewers",
        summary="How many people are watching",
        body=echo_or_custom("viewers", "{$channel.viewers} watching"),
    ),
    Derived(
        name="time",
        summary="The time where the channel is",
        body=echo_or_custom("time", "it's {$now.time} on {$now.weekday} here"),
    ),
    Derived(
        name="bot",
        summary="Which bot this is",
        body=echo_or_custom("bot", "I'm {$bot.name} v{$bot.version}"),
    ),
    Derived(
        name="nextstream",
        summary="When the next scheduled stream starts",
        body=(
            "ifelse {$channel.next_stream ?? false} ( "
            + echo_or_custom(
                "nextstream",
                "next stream in {$channel.next_stream[in]:human}: {$channel.next_stream[title] ?? untitled}"
                " ({$channel.next_stream[category] ?? no category})",
            )
            + " ) ( echo nothing on {$channel.display}'s schedule right now )"
        ),
    ),
    Derived(
        name="weather",
        summary="The weather somewhere right now",
        # wttr.in needs no key. `http` runs only in a bot admin's commands and only against hosts an admin
        # allowed (ADR-0020), so until both are true this fails with that reason and fetches nothing.
        body=(
            "http get https://wttr.in/{arg.place}?format=j1"
            " | echo {_1[nearest_area][0][areaName][0][value]}: {_1[current_condition][0][temp_C]}°C"
            " / {_1[current_condition][0][temp_F]}°F, {_1[current_condition][0][weatherDesc][0][value]}"
        ),
        declarations=('1+ name=place required=yes "a city or place, like Lisbon"',),
        note="needs the bot account to be a bot admin (`!admin add <bot>`) and `!admin http allow wttr.in`",
    ),
)


QUOTES_PACK = "quotes"
QUOTES_SUMMARY = "The channel's numbered quotes"
_Q, _N = "publisher.channel.quotes", "publisher.channel.quote_next"
_NO_ARG = '(arg.1 ?? "-")'  # `""` counts as missing (spec §7.3.3), so "no argument" compares as "-"

# A quote is a map in the bot's `publisher.channel.quotes`, keyed by its number: `text`, `date`, and `game`
# when the stream was live. `publisher.channel.quote_next` is the last number given out, so a deleted
# quote's number is never given again. Only this pack's commands write them (ADR-0019 item 9).
QUOTES: tuple[Derived, ...] = (
    Derived(
        name="quote",
        summary="Read, add and delete the channel's quotes",
        body=(
            f'ifelse {{{_NO_ARG} == "add"}} ( quote_add {{arg.2+raw}} )'
            f' ( ifelse {{{_NO_ARG} == "del" or {_NO_ARG} == "delete"}} ( quote_del {{arg.2}} )'
            f' ( ifelse {{{_NO_ARG} == "-"}} ( quote_random ) ( quote_show {{arg.1}} ) ) )'
        ),
        declarations=('1+ name=what required=no "a number, or add <text>, or del <number>"',),
    ),
    Derived(
        name="quote_add",
        summary="Add a quote (moderators)",
        body=(
            "ifelse {$chatter.is_mod}"
            " ( ifelse {arg.1+raw:len > 400} ( fail 2 a quote is at most 400 characters )"
            f" ( var incr {_N} && var set {_Q}[{{{_N}}}][text] {{arg.1+raw}}"
            f" && var set {_Q}[{{{_N}}}][date] {{$now.date}}"
            ' && ( ifelse {$channel.live and ($channel.game ?? "-") != "-"}'
            f" ( var set {_Q}[{{{_N}}}][game] {{$channel.game}} ) )"
            f" && echo added #{{{_N}}} ) )"
            " ( fail adding quotes takes moderator rank )"
        ),
        declarations=('1+ name=text required=yes "the quote"',),
        internal=True,
    ),
    Derived(
        name="quote_del",
        summary="Delete a quote (moderators); its number stays taken",
        body=(
            "ifelse {$chatter.is_mod}"
            f" ( ifelse {{({_Q}[arg.number] ?? 0) != 0}}"
            f" ( var del {_Q}[{{arg.number}}] && echo deleted #{{arg.number}} )"
            " ( fail 3 there is no quote #{arg.number} ) )"
            " ( fail deleting quotes takes moderator rank )"
        ),
        declarations=('1 name=number type=int required=yes "which quote, by number"',),
        internal=True,
    ),
    Derived(
        name="quote_show",
        summary="Say one quote",
        body=(
            f"ifelse {{({_Q}[arg.number] ?? 0) != 0}}"
            f' ( ifelse {{"game" in {_Q}[arg.number]}}'
            f" ( echo #{{arg.number}}: {{{_Q}[arg.number][text]}} [{{{_Q}[arg.number][game]}}, {{{_Q}[arg.number][date]}}] )"
            f" ( echo #{{arg.number}}: {{{_Q}[arg.number][text]}} [{{{_Q}[arg.number][date]}}] ) )"
            " ( fail 3 there is no quote #{arg.number} )"
        ),
        declarations=('1 name=number type=int required=yes "which quote, by number"',),
        internal=True,
    ),
    Derived(
        name="quote_random",
        summary="Say a random quote",
        body=(
            f"ifelse {{({_Q}:len ?? 0) > 0}}"
            f" ( random 1-{{{_Q}:len}} | quote_show {{{_Q}:keys[_1 - 1]}} )"
            " ( fail 3 no quotes yet. Add one with {$channel.prefix}quote add <text> )"
        ),
        internal=True,
    ),
)


PYRAMID_PACK = "pyramid"
PYRAMID_SUMMARY = "Break emote pyramids with a fact, congratulate the ones that finish, and keep stats"
_PY, _PS = "publisher.channel.pyramid", "publisher.channel.pyramid_stats"
_PF, _PSHARED = "publisher.channel.pyramid_facts", "publisher.pyramid_facts"
_NOBODY = 100000  # an exempt rank above bot_owner: nobody is exempt
_MOD_ONLY = "fail changing pyramid settings takes moderator rank"
_WHAT = '(arg.1 ?? "-")'
_IS_PYRAMID = '(event.type ?? "-") == "pyramid"'
_ATTEMPTED = f'({_PY}[attempt] ?? "-") == event.pyramid_id'
# A broken pyramid counts from one row short of the minimum, or whenever the bot tried to break it.
_COUNTS_BROKEN = f"event.peak >= ({_PY}[min_peak] ?? 3) - 1 or {_ATTEMPTED}"

# The pyramid chat watcher (ADR-0028) fires a `pyramid` trigger event on every row, on completion and on a
# break; a channel turns this on with `!event add pyramid pyramid_on_event`. Settings live in the bot's
# `publisher.channel.pyramid` map, totals in `publisher.channel.pyramid_stats`, per-chatter counts in
# `publisher.channel.chatter.pyramids` (built) and `.pyramid_breaks` (broke someone's), the channel's own
# facts in `publisher.channel.pyramid_facts`, and the shared facts in `publisher.pyramid_facts`, which
# `install` writes from PYRAMID_FACTS. Only this pack's commands write any of them.
PYRAMID: tuple[Derived, ...] = (
    Derived(
        name="pyramid",
        summary="Pyramid stats, the top builders and breakers, facts and settings",
        body=(
            f'ifelse {{{_WHAT} == "-" or {_WHAT} == "stats"}} ( pyramid_stats )'
            f' ( ifelse {{{_WHAT} == "top"}} ( pyramid_top {{arg.2 ?? builders}} )'
            f' ( ifelse {{{_WHAT} == "fact"}} ( pyramid_fact {{arg.2 ?? 0}} )'
            f' ( ifelse {{{_WHAT} == "settings"}} ( pyramid_settings )'
            f' ( ifelse {{{_WHAT} == "addfact"}} ( pyramid_addfact {{arg.2+raw ?? -}} )'
            f' ( ifelse {{{_WHAT} == "delfact"}} ( pyramid_delfact {{arg.2 ?? 0}} )'
            f' ( ifelse {{{_WHAT} == "exempt"}} ( pyramid_exempt {{arg.2 ?? -}} )'
            " ( pyramid_set {arg.1} {arg.2+ ?? -} ) ) ) ) ) ) )"
        ),
        declarations=(
            '1+ name=what required=no "stats, top [breakers], fact [number], settings, or a setting:'
            ' chance, minpeak, exempt, congrats, sharedfacts, addfact, delfact"',
        ),
    ),
    Derived(
        name="pyramid_on_event",
        summary="Reacts to pyramid trigger events: !event add pyramid pyramid_on_event",
        body=(
            f"ifelse {{{_IS_PYRAMID}}}"
            ' ( ifelse {event.phase == "step"} ( pyramid_step )'
            ' ( ifelse {event.phase == "complete"} ( pyramid_complete ) ( pyramid_broken ) ) )'
        ),
        note="a channel turns it on with `!event add pyramid pyramid_on_event`, then `!pyramid chance <percent>`",
    ),
    Derived(
        name="pyramid_step",
        summary="Maybe break a pyramid with a fact",
        # One try per pyramid: once the bot's line is out, it breaks the pyramid wherever it lands.
        body=(
            f"ifelse {{$chatter.rank < ({_PY}[exempt_rank] ?? {_NOBODY}) and not ({_ATTEMPTED})}}"
            f" ( random 1-100 | ifelse {{_ <= ({_PY}[chance] ?? 0)}}"
            f" ( ( pyramid_fact 0 -> {_PY}[last_fact] && var set {_PY}[attempt] {{event.pyramid_id}}"
            f" && echo {{{_PY}[last_fact][value]}} ) || true ) )"
        ),
        internal=True,
    ),
    Derived(
        name="pyramid_complete",
        summary="Count a finished pyramid and congratulate its builder",
        body=(
            f"ifelse {{event.peak >= ({_PY}[min_peak] ?? 3)}}"
            f" ( var incr {_PS}[completed] && var incr publisher.channel.chatter.pyramids"
            f" && ( ifelse {{event.peak > ({_PS}[biggest] ?? 0)}}"
            f" ( var set {_PS}[biggest] {{event.peak}} && var set {_PS}[biggest_by] {{event.user.display}} ) )"
            f" && ( ifelse {{{_ATTEMPTED}}} ( var incr {_PS}[dodged] ) )"
            f' && ifelse {{({_PY}[congrats] ?? "-") != "off"}}'
            f" ( echo {{{_PY}[congrats]:template ?? {{event.user.display}} built a {{event.peak}}-wide"
            " {event.token} pyramid!} ) ( true ) )"
        ),
        internal=True,
    ),
    Derived(
        name="pyramid_broken",
        summary="Count a broken pyramid: by the bot, by another chatter, or fumbled by its builder",
        # The run's chatter is whoever broke it (ADR-0028), so `pyramid_breaks` lands on the breaker.
        body=(
            f"ifelse {{{_COUNTS_BROKEN}}}"
            f" ( ifelse {{event.by_bot}} ( var incr {_PS}[broken_by_bot] && true )"
            f" ( ifelse {{event.self_broken}} ( var incr {_PS}[fumbled] && true )"
            f" ( var incr {_PS}[broken_by_chatters] && var incr publisher.channel.chatter.pyramid_breaks && true ) ) )"
        ),
        internal=True,
    ),
    Derived(
        name="pyramid_fact",
        summary="Say a random pyramid fact, or the channel's fact by number",
        body=(
            "ifelse {(arg.number ?? 0) > 0}"
            f" ( ifelse {{arg.number <= ({_PF}:len ?? 0)}} ( echo #{{arg.number}}: {{{_PF}[arg.number - 1]}} )"
            " ( fail 3 there is no pyramid fact #{arg.number} ) )"
            f" ( ifelse {{({_PY}[shared_facts] ?? true) and ({_PSHARED}:len ?? 0) > 0}}"
            f" ( ifelse {{({_PF}:len ?? 0) > 0}} ( random {{{_PF} + {_PSHARED}}} ) ( random {{{_PSHARED}}} ) )"
            f" ( ifelse {{({_PF}:len ?? 0) > 0}} ( random {{{_PF}}} )"
            " ( fail 3 no pyramid facts yet. Add one with {$channel.prefix}pyramid addfact <fact> ) ) )"
        ),
        declarations=('1 name=number type=int required=no "the channel\'s fact by number; none for a random one"',),
        internal=True,
    ),
    Derived(
        name="pyramid_stats",
        summary="Say the channel's pyramid totals",
        body=(
            f"echo pyramids: {{{_PS}[completed] ?? 0}} built, {{{_PS}[broken_by_bot] ?? 0}} broken by me,"
            f" {{{_PS}[broken_by_chatters] ?? 0}} broken by chat, {{{_PS}[fumbled] ?? 0}} fumbled,"
            f" {{{_PS}[dodged] ?? 0}} got past me"
            f" | ifelse {{({_PS}[biggest] ?? 0) > 0}}"
            f" ( echo {{_}}. Biggest: {{{_PS}[biggest]}} wide by {{{_PS}[biggest_by]}} ) ( echo {{_}} )"
        ),
        internal=True,
    ),
    Derived(
        name="pyramid_top",
        summary="Say the top pyramid builders or breakers",
        body=(
            'ifelse {arg.board == "breakers"}'
            " ( var top publisher.channel.chatter.pyramid_breaks | ifelse {_:len > 0}"
            " ( echo top pyramid breakers: {_.message} ) ( echo nobody has broken a pyramid yet ) )"
            " ( var top publisher.channel.chatter.pyramids | ifelse {_:len > 0}"
            " ( echo top pyramid builders: {_.message} ) ( echo nobody has built a pyramid yet ) )"
        ),
        declarations=('1 name=board required=yes "builders or breakers"',),
        internal=True,
    ),
    Derived(
        name="pyramid_settings",
        summary="Say the channel's pyramid settings",
        body=(
            f"echo break chance {{{_PY}[chance] ?? 0}}% per row, pyramids count from {{{_PY}[min_peak] ?? 3}} wide,"
            f" exempt: {{{_PY}[exempt] ?? off}}, shared facts: {{{_PY}[shared_facts] ?? true}},"
            f" channel facts: {{{_PF}:len ?? 0}},"
            f' congratulations: {{({_PY}[congrats] ?? "default") == "off" and "off" or "on"}}'
        ),
        internal=True,
    ),
    Derived(
        name="pyramid_set",
        summary="Change a pyramid setting (moderators)",
        body=(
            f"ifelse {{not $chatter.is_mod}} ( {_MOD_ONLY} )"
            ' ( ifelse {arg.1 == "chance"}'
            " ( ifelse {0 <= (arg.2:int ?? -1) <= 100}"
            f" ( var set {_PY}[chance] {{arg.2:int}} && echo pyramid break chance is now {{arg.2:int}}% per row )"
            " ( fail 2 usage: pyramid chance <0-100> ) )"
            ' ( ifelse {arg.1 == "minpeak"}'
            " ( ifelse {2 <= (arg.2:int ?? 0) <= 50}"
            f" ( var set {_PY}[min_peak] {{arg.2:int}} && echo pyramids now count from {{arg.2:int}} wide )"
            " ( fail 2 usage: pyramid minpeak <2-50> ) )"
            ' ( ifelse {arg.1 == "sharedfacts"}'
            ' ( ifelse {arg.2 == "on" or arg.2 == "off"}'
            f' ( var set {_PY}[shared_facts] {{arg.2 == "on"}} && echo shared pyramid facts are {{arg.2}} )'
            " ( fail 2 usage: pyramid sharedfacts <on|off> ) )"
            ' ( ifelse {arg.1 == "congrats" and arg.2 != "-"}'
            ' ( ifelse {arg.2 == "reset"}'
            f" ( ( var del {_PY}[congrats] || true ) && echo pyramid congratulations are back to the default )"
            f' ( var set {_PY}[congrats] {{arg.2+raw}} && ifelse {{arg.2 == "off"}}'
            " ( echo pyramid congratulations are off ) ( echo pyramid congratulations set ) ) )"
            " ( fail 2 usage: pyramid <stats|top|fact|settings|chance|minpeak|exempt|congrats|sharedfacts"
            "|addfact|delfact> ) ) ) ) )"
        ),
        declarations=(
            '1 name=setting required=yes "chance, minpeak, congrats or sharedfacts"',
            '2+ name=value required=yes "the new value"',
        ),
        internal=True,
    ),
    Derived(
        name="pyramid_exempt",
        summary="Set the rank that is never broken (moderators)",
        body=(
            f"ifelse {{not $chatter.is_mod}} ( {_MOD_ONLY} )"
            f' ( ( ifelse {{arg.role == "off"}} ( var set {_PY}[exempt_rank] {_NOBODY} )'
            f' ( ifelse {{arg.role == "broadcaster"}} ( var set {_PY}[exempt_rank] 100 )'
            f' ( ifelse {{arg.role == "mod"}} ( var set {_PY}[exempt_rank] 80 )'
            f' ( ifelse {{arg.role == "vip"}} ( var set {_PY}[exempt_rank] 60 )'
            f' ( ifelse {{arg.role == "sub"}} ( var set {_PY}[exempt_rank] 20 )'
            " ( fail 2 usage: pyramid exempt <off|sub|vip|mod|broadcaster> ) ) ) ) ) )"
            f" && var set {_PY}[exempt] {{arg.role}}"
            ' && ifelse {arg.role == "off"} ( echo nobody is exempt from pyramid breaks )'
            " ( echo {arg.role} and up are exempt from pyramid breaks ) )"
        ),
        declarations=('1 name=role required=yes "off, sub, vip, mod or broadcaster"',),
        internal=True,
    ),
    Derived(
        name="pyramid_addfact",
        summary="Add a pyramid fact for this channel (moderators)",
        body=(
            f"ifelse {{not $chatter.is_mod}} ( {_MOD_ONLY} )"
            ' ( ifelse {arg.1+raw == "-"} ( fail 2 usage: pyramid addfact <fact> )'
            " ( ifelse {arg.1+raw:len > 300} ( fail 2 a pyramid fact is at most 300 characters )"
            f" ( echo {{arg.1+raw}} --> {_PF} && echo added pyramid fact #{{{_PF}:len}} ) ) )"
        ),
        declarations=('1+ name=fact required=yes "the fact"',),
        internal=True,
    ),
    Derived(
        name="pyramid_delfact",
        summary="Delete one of the channel's pyramid facts (moderators)",
        body=(
            f"ifelse {{not $chatter.is_mod}} ( {_MOD_ONLY} )"
            f" ( ifelse {{1 <= arg.number <= ({_PF}:len ?? 0)}}"
            f" ( var pop {_PF} {{arg.number - 1}} && echo deleted pyramid fact #{{arg.number}} )"
            " ( fail 3 there is no pyramid fact #{arg.number} ) )"
        ),
        declarations=('1 name=number type=int required=yes "which fact, by number"',),
        internal=True,
    ),
)

# The shared facts every channel draws from unless it turns them off with `!pyramid sharedfacts off`. This
# list is their source: `install` rewrites `publisher.pyramid_facts` whenever it changes.
PYRAMID_FACTS: tuple[str, ...] = (
    "The Great Pyramid of Giza was the tallest human-made structure on Earth for over 3,800 years.",
    "The Great Pyramid was built for the pharaoh Khufu, around 2560 BC.",
    "The Great Pyramid was about 146 meters tall when finished. It has lost around 8 meters off its top.",
    "The Great Pyramid is made of an estimated 2.3 million stone blocks.",
    "The sides of the Great Pyramid line up with north, south, east and west to within a fraction of a degree.",
    "The Great Pyramid was once covered in polished white limestone casing stones.",
    "Most of the Great Pyramid's casing stones were carted off in the Middle Ages to build Cairo.",
    "The Great Pyramid is the only one of the Seven Wonders of the Ancient World still standing.",
    "The granite in the Great Pyramid's King's Chamber came from Aswan, about 800 km up the Nile.",
    "The Diary of Merer, among the oldest inscribed papyri ever found, logs limestone shipped for the Great Pyramid.",
    "Workers' villages at Giza suggest the pyramids were built by paid laborers, not slaves.",
    "Nobody knows for sure how the pyramid blocks were raised. Ramps are the leading theory.",
    "The Pyramid of Djoser at Saqqara, a step pyramid from around 2670 BC, is the oldest Egyptian pyramid.",
    "Imhotep is credited as the architect of the Step Pyramid of Djoser.",
    "The Bent Pyramid at Dahshur changes its angle partway up, from about 54 degrees to about 43.",
    "The Red Pyramid at Dahshur is thought to be Egypt's first successful smooth-sided pyramid.",
    "Sneferu, Khufu's father, built at least three pyramids.",
    "The Pyramid of Khafre looks taller than the Great Pyramid because it stands on higher ground.",
    "The Pyramid of Khafre still has some of its original casing stones near the top.",
    "The Pyramid of Menkaure is the smallest of the three main pyramids at Giza.",
    "Egypt has well over 100 known pyramids.",
    "Sudan has more pyramids than Egypt: over 200, built by the Kingdom of Kush.",
    "Nearly every Egyptian pyramid was emptied by tomb robbers in ancient times.",
    "Nearly all of Egypt's pyramids are on the west bank of the Nile, the side of the setting sun.",
    "The last royal pyramid built in Egypt is thought to be that of Ahmose I, at Abydos.",
    "The pyramidion was the capstone at the very top of a pyramid, sometimes covered in gold.",
    "The word pyramid comes from the Greek pyramis.",
    "The Great Pyramid of Cholula in Mexico is the largest pyramid in the world by volume.",
    "The Great Pyramid of Cholula is so overgrown it looks like a hill, with a church on top.",
    "El Castillo at Chichen Itza has 91 steps on each side. With the top platform, that makes 365.",
    "At the equinoxes, shadows on El Castillo's stairs look like a serpent sliding down.",
    "The Pyramid of the Sun at Teotihuacan is one of the largest buildings in the ancient Americas.",
    "The Pyramid of Cestius in Rome was built as a tomb, around 12 BC.",
    "The Louvre Pyramid in Paris, designed by I. M. Pei, opened in 1989.",
    "The Louvre Pyramid is made of 673 panes of glass.",
    "The Luxor hotel in Las Vegas is a 30-story pyramid with a beam of light shining from its tip.",
    "The Transamerica Pyramid in San Francisco is 260 meters tall.",
    "A square pyramid has 5 faces, 8 edges and 5 corners.",
    "A pyramid's volume is a third of its base area times its height.",
    "A triangular pyramid with four equal equilateral faces is a regular tetrahedron.",
)


def check_core() -> None:
    """Every `core` body may call only what can never be switched off (ADR-0019). Raises NotASentinel."""
    registry = builtin_registry()
    names = [d.name for d in CORE_COMMANDS]
    for derived in CORE_COMMANDS:
        if registry.get(derived.name) is not None:
            raise NotASentinel(f"{derived.name} is a built-in already")
        body = CustomCommandService.parse_body(derived.body, DEFAULT_PREFIX)
        check_sentinel_body(derived.name, body, registry, names)


async def install(conn: Connection, *, owner_user_id: str, owner_login: str, dry_run: bool = False) -> list[str]:
    """Create or update `core` and the starter commands, and publish the starter pack globally. Returns
    what it did. Raises NotASentinel before changing anything if a `core` body isn't a sentinel's."""
    check_core()
    filters = FilterService(conn)
    await filters.reload()  # the global list: these bodies are read out in every channel
    commands = CustomCommandService(conn, filters=filters)
    packs = PackService(conn, commands)
    owner = (owner_user_id, owner_login)
    done = await _install(commands, packs, owner, CORE, CORE_SUMMARY, CORE_COMMANDS, CORE_VERSION, dry_run=dry_run)
    done += await _install(commands, packs, owner, PACK, PACK_SUMMARY, STARTER, None, dry_run=dry_run)
    done += await _install(commands, packs, owner, QUOTES_PACK, QUOTES_SUMMARY, QUOTES, None, dry_run=dry_run)
    done += await _install(commands, packs, owner, PYRAMID_PACK, PYRAMID_SUMMARY, PYRAMID, None, dry_run=dry_run)
    done += await _install_facts(conn, owner_user_id, dry_run=dry_run)
    return done


async def _install_facts(conn: Connection, owner_user_id: str, *, dry_run: bool) -> list[str]:
    """Write PYRAMID_FACTS to the bot's `publisher.pyramid_facts`, which the pyramid pack reads. Too long
    for a command body, and no command writes it, so this list is its only source."""
    facts = list(PYRAMID_FACTS)
    stored = await fetch_value(
        conn,
        "SELECT value FROM variables WHERE ns = 'publisher' AND key1 = %s AND key2 = '' AND key3 = ''"
        " AND name = 'pyramid_facts'",
        (owner_user_id,),
    )
    if stored is not None and json.loads(stored) == facts:
        return []
    if not dry_run:
        await conn.execute(
            "INSERT INTO variables (ns, key1, key2, key3, name, value, updated_at, updated_via)"
            " VALUES ('publisher', %s, '', '', 'pyramid_facts', %s, %s, 'script')"
            " ON CONFLICT (ns, key1, key2, key3, name) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at, updated_by = NULL, updated_via = excluded.updated_via",
            (owner_user_id, json.dumps(facts), now_ms()),
        )
    return [f"{'set' if stored is None else 'update'} the {len(facts)} shared pyramid facts"]


async def _install(
    commands: CustomCommandService,
    packs: PackService,
    owner: tuple[str, str],
    name: str,
    summary: str,
    derived_commands: tuple[Derived, ...],
    system_version: int | None,
    *,
    dry_run: bool,
) -> list[str]:
    """One pack: a system pack when `system_version` is set, else published globally."""
    owner_user_id, owner_login = owner
    done: list[str] = []
    pack: Pack | None = await packs.by_owner(owner_user_id, name)
    if pack is None:
        done.append(f"create {'system ' if system_version else ''}pack {name}")
        if not dry_run:
            pack = await packs.create(
                owner_user_id=owner_user_id,
                name=name,
                summary=summary,
                actor_via="script",
                system_version=system_version,
                replaces_module=name in RESERVED_PACK_NAMES,
            )
    members = {c.name for c in await packs.members(pack.id)} if pack is not None else set()
    internal = await packs.internal_names(pack.id) if pack is not None else set()

    for derived in derived_commands:
        command = await commands.by_owner(owner_user_id, derived.name)
        declared = derived.params()
        if command is None:
            done.append(f"add {derived.name}")
        elif command.body != derived.body:
            done.append(f"edit {derived.name}")
        elif command.params != declared or command.summary != derived.summary:
            done.append(f"redescribe {derived.name}")
        if derived.name not in members:
            done.append(f"put {derived.name} in {name}")
        if derived.internal != (derived.name in internal):
            done.append(f"make {derived.name} {'internal' if derived.internal else 'public'}")
        if dry_run:
            continue

        if command is None:
            command = await commands.create(
                owner_user_id=owner_user_id,
                owner_login=owner_login,
                name=derived.name,
                body=derived.body,
                channel_id=GLOBAL,
                prefix=DEFAULT_PREFIX,
                actor_via="script",
            )
        elif command.body != derived.body:
            command = await commands.edit(
                command, derived.body, channel_id=GLOBAL, prefix=DEFAULT_PREFIX, actor_via="script"
            )
        if command.params != declared:
            command = await commands.set_params(command, list(declared), actor_via="script")
        if command.summary != derived.summary:
            await commands.set_summary(command, derived.summary, actor_via="script")
        if pack is not None and derived.name not in members:
            await packs.add_member(pack, command, actor_via="script")
        if pack is not None and derived.internal != (derived.name in internal):
            await packs.set_internal(pack, command, derived.internal, actor_via="script")

    if system_version is not None:  # it resolves everywhere unpublished; startup checks the version
        if pack is not None and (pack.system_version or 0) < system_version:
            done.append(f"mark {name} version {system_version}")
            if not dry_run:
                await packs.set_system_version(pack, system_version)
        return done
    if pack is None or not any(p.pack_id == pack.id for p, _ in await packs.publications_in(GLOBAL)):
        done.append(f"publish {name} globally")
    if pack is not None and not dry_run:
        await packs.publish(channel_id=GLOBAL, pack=pack, published_by=owner_user_id, actor_via="script")
    return done


async def bot_account(conn: Connection) -> tuple[str, str] | None:
    """The bot's own account, which is who these commands come from."""
    async with await conn.execute("SELECT user_id, login FROM oauth_tokens WHERE identity = 'bot'") as cur:
        row = await cur.fetchone()
    return (row["user_id"], row["login"]) if row else None


async def run(args: argparse.Namespace) -> int:
    try:
        conn = await connect(args.database_url or Settings().database_dsn(), "bot")
    except psycopg.OperationalError as exc:
        print(f"cannot reach the database: {exc}", file=sys.stderr)
        return 2
    try:
        # The migrate step upgrades the schema just before this runs (ADR-0022). Run alone, this checks
        # it instead: the pack is written for this build's columns.
        try:
            await check_schema(conn, "bot")
        except SchemaMismatch as exc:
            print(f"stopped: {exc}", file=sys.stderr)
            return 1
        owner = (args.owner_id, args.owner_login) if args.owner_id and args.owner_login else None
        owner = owner or await bot_account(conn)
        if owner is None:
            print(
                "no bot account in this database yet: sign the bot in first, or pass --owner-id and --owner-login",
                file=sys.stderr,
            )
            return 2
        try:
            done = await install(conn, owner_user_id=owner[0], owner_login=owner[1], dry_run=args.dry_run)
        except (CustomCommandError, NotASentinel) as exc:
            print(f"stopped: {exc}", file=sys.stderr)
            return 1
    finally:
        await conn.close()

    changes = ", ".join(done) if done else "nothing to do"
    print(f"{'would: ' if args.dry_run else ''}{changes} (as @{owner[1]})")
    for derived in (*STARTER, *PYRAMID):
        if derived.note:
            print(f"  {derived.name}: {derived.note}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", default=None, help="Postgres URL (default: the bot's own DATABASE_URL)")
    parser.add_argument("--owner-id", default="", help="Twitch user ID to own the commands")
    parser.add_argument("--owner-login", default="", help="that account's login")
    parser.add_argument("--dry-run", action="store_true", help="say what would change, change nothing")
    args = parser.parse_args(argv)
    configure_event_loop()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
