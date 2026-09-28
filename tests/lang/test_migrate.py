"""Golden pairs for the 1.0 → 2.0 rewrite of stored bodies (ADR-0018 item 7)."""

from __future__ import annotations

import pytest

from doomtp_bot.lang.migrate import migrate_v1
from doomtp_bot.lang.parser import Context, ParserParams, parse

GOLDEN = [
    # result refs
    ("echo {1}", "echo {_1}"),
    ("echo {2.name} {1.data}", "echo {_2[name]} {_1.data}"),
    ("echo {_} {_.code} {_.x.y}", "echo {_} {_.code} {_[x][y]}"),
    # bot fields get `$`; variables don't
    ("echo {chatter.display} {channel.uptime}", "echo {$chatter.display} {$channel.uptime}"),
    (
        "echo {publisher.display} {bot.version} {now.time}",
        "echo {$publisher.display} {$bot.version} {$now.time}",
    ),
    ("echo {channel.deaths} {chatter.pts}", "echo {channel.deaths} {chatter.pts}"),
    # paths inside a value
    ("echo {channel.stats.kills} {channel.log.0}", "echo {channel.stats[kills]} {channel.log[0]}"),
    ("echo {channel.chatter.x} {publisher.chatter.y}", "echo {channel.chatter.x} {publisher.chatter.y}"),
    ("echo {publisher.channel.chatter.pts.best}", "echo {publisher.channel.chatter.pts[best]}"),
    ("echo {arg.streamer.name} {arg.1+} {args}", "echo {arg.streamer[name]} {arg.1+} {args}"),
    ("echo {event.user.name} {match.1}", "echo {event.user.name} {match.1}"),
    ("echo {channel.x._2}", 'echo {channel.x["_2"]}'),
    # casts and fallbacks, nested placeholders too
    ("echo {arg.1:int ?? 0} {arg.1:choice(a,b)}", "echo {arg.1:int ?? 0} {arg.1:choice(a,b)}"),
    ("echo {arg.1 ?? the chat, {chatter.name}}", "echo {arg.1 ?? the chat, {$chatter.name}}"),
    # store and append
    ("random 1-6 > channel.last", "random 1-6 -> channel.last"),
    ("echo hi >> channel.log && echo {1}", "echo hi --> channel.log && echo {_1}"),
    ("( echo a | echo {_} ) > chatter.x", "( echo a | echo {_} ) -> chatter.x"),
    # text stays text
    ('echo "a > b {1}"', 'echo "a > b {_1}"'),
    ("echo a->b 3>2", "echo a->b 3>2"),
    ("echo -> -->", 'echo "->" "-->"'),
    ("echo \\{1} \\>", "echo \\{1} \\>"),
]


@pytest.mark.parametrize(("v1", "v2"), GOLDEN)
def test_rewrite(v1: str, v2: str) -> None:
    assert migrate_v1(v1) == v2
    parse(v2, Context.BODY, ParserParams(prefix="!"))
