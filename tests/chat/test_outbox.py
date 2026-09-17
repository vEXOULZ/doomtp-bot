"""Outbox: chunking, rate limiting, TTL, moderation recheck, duplicates, filter hook."""

from __future__ import annotations

from dataclasses import dataclass, field

from doomtp_bot.core.outbox import DUPLICATE_SUFFIX, Outbox, SendResult, chunk


@dataclass
class FakeTime:
    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class FakeSender:
    sent: list[tuple[str, str, str | None]] = field(default_factory=list)
    reject: bool = False

    async def send_chat(self, channel_id: str, text: str, reply_to: str | None) -> SendResult:
        self.sent.append((channel_id, text, reply_to))
        if self.reject:
            return SendResult("x", "twitch_rejected:msg_duplicate")
        return SendResult(f"t{len(self.sent)}")


@dataclass
class FakeLog:
    rows: list[dict[str, object]] = field(default_factory=list)

    async def outbound(self, **kwargs: object) -> None:
        self.rows.append(kwargs)


def test_chunking() -> None:
    assert chunk("a  b\n c") == ["a b c"]
    long = " ".join(["word"] * 200)  # 999 chars
    parts = chunk(long)
    assert len(parts) == 2 and all(len(p) <= 500 for p in parts)
    too_long = " ".join(["word"] * 400)
    parts = chunk(too_long)
    assert len(parts) == 2 and parts[-1].endswith("…") and len(parts[-1]) <= 500
    assert chunk("x" * 1200) == ["x" * 500, "x" * 499 + "…"]


async def test_reply_threads_only_first_chunk_and_logs() -> None:
    sender, out_log, t = FakeSender(), FakeLog(), FakeTime()
    outbox = Outbox(sender, out_log, clock=t.clock, sleep=t.sleep)
    results = await outbox.send("c1", " ".join(["word"] * 200), reply_to="m1")
    assert [r.dropped_reason for r in results] == [None, None]
    assert [s[2] for s in sender.sent] == ["m1", None]
    assert len(out_log.rows) == 2 and out_log.rows[0]["twitch_message_id"] == "t1"


async def test_token_bucket_waits_then_ttl_drops() -> None:
    sender, t = FakeSender(), FakeTime()
    outbox = Outbox(sender, rate_for=lambda c: (2, 30.0), ttl_s=20.0, clock=t.clock, sleep=t.sleep)
    for i in range(3):
        await outbox.send("c1", f"msg {i}")
    assert len(sender.sent) == 3 and t.sleeps == [15.0]  # third message waited for a token
    # A queue so long that waiting would exceed the TTL: dropped instead of sent late.
    outbox_short = Outbox(FakeSender(), rate_for=lambda c: (1, 60.0), ttl_s=5.0, clock=t.clock, sleep=t.sleep)
    assert (await outbox_short.send("c1", "a"))[0].dropped_reason is None
    assert (await outbox_short.send("c1", "b"))[0].dropped_reason == "ttl"


async def test_moderation_recheck_right_before_sending() -> None:
    sender, out_log, t = FakeSender(), FakeLog(), FakeTime()
    outbox = Outbox(sender, out_log, hold_ms_for=lambda c: 500, clock=t.clock, sleep=t.sleep)
    deleted = {"yes": False}

    async def hold(seconds: float) -> None:
        deleted["yes"] = True  # a mod deletes the message during the reply hold
        t.now += seconds

    outbox.sleep = hold
    results = await outbox.send("c1", "reply", reply_to="m1", is_invalidated=lambda: deleted["yes"])
    assert results[0].dropped_reason == "moderated" and sender.sent == []
    assert out_log.rows[0]["dropped_reason"] == "moderated"


async def test_duplicate_messages_get_invisible_suffix() -> None:
    sender = FakeSender()
    outbox = Outbox(sender)
    await outbox.send("c1", "same")
    await outbox.send("c1", "same")
    await outbox.send("c2", "same")
    assert [s[1] for s in sender.sent] == ["same", "same" + DUPLICATE_SUFFIX, "same"]


async def test_filter_can_rewrite_or_block() -> None:
    sender, out_log = FakeSender(), FakeLog()

    def content_filter(channel_id: str, text: str) -> tuple[str | None, list[str]]:
        if "blocked" in text:
            return None, ["rule:1"]
        return text.replace("heck", "****"), (["rule:2"] if "heck" in text else [])

    outbox = Outbox(sender, out_log, content_filter=content_filter)
    await outbox.send("c1", "what the heck")
    assert sender.sent[-1][1] == "what the ****" and out_log.rows[-1]["text_prefilter"] == "what the heck"
    assert (await outbox.send("c1", "blocked words"))[0].dropped_reason == "filter_block"


async def test_twitch_rejections_are_recorded() -> None:
    sender = FakeSender(reject=True)
    outbox = Outbox(sender)
    result = await outbox.send("c1", "x")
    assert result[0].dropped_reason == "twitch_rejected:msg_duplicate"
    assert outbox.dropped == {"twitch_rejected:msg_duplicate": 1}
