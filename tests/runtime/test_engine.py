"""Runtime behaviour: spec Appendix A.2 plus operators, sentinels, placeholders, limits."""

from __future__ import annotations

import random

import pytest

from doomtp_bot.lang.parser import Context
from doomtp_bot.runtime.context import Publisher
from doomtp_bot.runtime.executor import ScopeArgs
from doomtp_bot.runtime.policy import AllowAllPolicy, Decision
from doomtp_bot.runtime.result import Code, Result
from doomtp_bot.runtime.spec import CommandSpec
from doomtp_bot.runtime.variables import InMemoryVariableStore, VarKey
from tests.runtime.helpers import ALICE, CHANNEL, make_runtime, run


# ── Appendix A.2 ───────────────────────────────────────────────────────────
async def test_a2_1_pipe_data_path() -> None:
    r = await run(make_runtime(), '!weather Lisbon | echo "it\'s {1.celsius}C"')
    assert (r.result.code, r.send) == (0, "it's 21.5C")


async def test_a2_2_pipe_stops_on_failure() -> None:
    r = await run(make_runtime(), '!weather Nowhere | echo "{1.celsius}"')
    assert (r.result.code, r.send, r.executed) == (3, "location not found", [1])


async def test_a2_3_or_handles_failure() -> None:
    r = await run(make_runtime(), '!weather Nowhere || echo "failed: {_.message}"')
    assert (r.result.code, r.send) == (0, "failed: location not found")


async def test_a2_4_grouped_fallback_is_stored() -> None:
    store = InMemoryVariableStore()
    r = await run(make_runtime(store=store), "( !weather Nowhere || default ? ) > chatter.w")
    assert (r.result.code, r.send) == (0, "?")
    assert store.data[VarKey("chatter", "u1", name="w")] == "?"


async def test_a2_5_store_skipped_on_failure() -> None:
    store = InMemoryVariableStore()
    r = await run(make_runtime(store=store), "!weather Nowhere > chatter.w")
    assert (r.result.code, r.send, store.data) == (3, "location not found", {})


async def test_a2_6_reference_to_skipped_command_is_missing() -> None:
    r = await run(make_runtime(), "!ping || !random 1-6 && echo {2}")
    assert r.result.code == Code.USAGE and r.send == "missing value: {2}"


async def test_a2_7_true_makes_optional_and_sends_nothing() -> None:
    r = await run(make_runtime(), "!weather Nowhere || true")
    assert (r.result.code, r.send) == (0, None)


async def test_a2_8_unknown_first_command_is_silent() -> None:
    r = await run(make_runtime(), "!foo")
    assert (r.result.code, r.send, r.origin) == (127, None, "preflight")


async def test_a2_9_unknown_later_command_replies() -> None:
    r = await run(make_runtime(), "!random 1-6 | !foo")
    assert (r.result.code, r.send, r.executed) == (127, "unknown command: foo", [])


class DenyAdd(AllowAllPolicy):
    """`add` needs a moderator, refused in preflight; `ping` is on cooldown, refused when it is reached."""

    def check(self, ctx, spec: CommandSpec) -> Decision:  # type: ignore[no-untyped-def]
        if spec.name == "add":
            return Decision(False, Code.DENIED, "needs mod", {"required_role": "moderator"})
        return Decision.allow()

    def is_permitted(self, ctx, spec: CommandSpec) -> bool:  # type: ignore[no-untyped-def]
        return spec.name != "add"

    def check_cooldown(self, ctx, spec: CommandSpec) -> Decision:  # type: ignore[no-untyped-def]
        if spec.name == "ping":
            return Decision(False, Code.COOLDOWN, "cooldown", {"command": "ping", "user_remaining": 4})
        return Decision.allow()


class LosesTheRace(AllowAllPolicy):
    """The early look finds the bucket free, and by the time the claim comes someone else has it."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def check_cooldown(self, ctx, spec: CommandSpec) -> Decision:  # type: ignore[no-untyped-def]
        self.calls.append("look")
        return Decision.allow()

    def claim_cooldown(self, ctx, spec: CommandSpec) -> Decision:  # type: ignore[no-untyped-def]
        self.calls.append("claim")
        return Decision(False, Code.COOLDOWN, "cooldown", {"command": spec.name})


async def test_a2_10_denied_is_silent_with_callback() -> None:
    r = await run(make_runtime(policy=DenyAdd()), "!add 1 2")
    assert (r.result.code, r.send, r.callback, r.result.data) == (
        126,
        None,
        "on_denied",
        {"required_role": "moderator"},
    )  # type: ignore[union-attr]


# ── cooldowns fail the invocation, at runtime (spec 1.1, ADR-0006 item 5) ────
async def test_cooldown_is_silent_with_callback() -> None:
    r = await run(make_runtime(policy=DenyAdd()), "!ping")
    assert (r.result.code, r.send, r.callback, r.origin, r.executed) == (
        128,
        None,
        "on_cooldown",
        "runtime",
        [],
    )
    assert r.result.data == {"command": "ping", "user_remaining": 4}  # what {cooldown.*} is built from


async def test_a_branch_that_never_runs_never_trips_its_cooldown() -> None:
    # Under 1.0 preflight checked every invocation, so this whole line failed with 128 even though the
    # `||` means `!ping` is never reached.
    r = await run(make_runtime(policy=DenyAdd()), "!random 1-6 || !ping")
    assert (r.result.code, r.callback, r.executed) == (0, None, [1])


async def test_or_routes_around_a_cooldown() -> None:
    r = await run(make_runtime(policy=DenyAdd()), "!ping || echo ping is resting for {_.user_remaining}s")
    assert (r.result.code, r.send, r.callback, r.executed) == (0, "ping is resting for 4s", None, [2])


async def test_and_stops_at_a_cooldown() -> None:
    r = await run(make_runtime(policy=DenyAdd()), "!ping && echo never")
    assert (r.result.code, r.send, r.callback, r.executed) == (128, None, "on_cooldown", [])


async def test_the_claim_just_before_running_is_the_one_that_counts() -> None:
    policy = LosesTheRace()
    r = await run(make_runtime(policy=policy), "!ping")
    assert (r.result.code, r.executed) == (128, [])
    assert policy.calls == ["look", "claim"]  # looked, expanded the arguments, then claimed — and lost


class SeesCallbackRuns(DenyAdd):
    """DenyAdd, remembering which run each invocation it checks belongs to."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str | None, bool]] = []

    def check(self, ctx, spec: CommandSpec) -> Decision:  # type: ignore[no-untyped-def]
        self.seen.append((spec.name, ctx.trigger_id, ctx.dry_run))
        return super().check(ctx, spec)


class EchoCallback:
    def callback_expr(self, ctx, command, module, kind) -> str:  # type: ignore[no-untyped-def]
        return "!echo resting"


async def test_a_callback_belongs_to_the_run_that_raised_it() -> None:
    # The callback runs inside the same trigger (so its cooldowns are that trigger's buckets) and inside the
    # same `!explain --run` (nothing it writes is kept, spec §9).
    policy = SeesCallbackRuns()
    r = await run(
        make_runtime(policy=policy, callbacks=EchoCallback()), "!ping", trigger_id="7", dry_run=True
    )
    assert (r.result.code, r.send) == (128, "resting")
    assert policy.seen == [("ping", "7", True), ("echo", "7", True)]


class DenyWrites:
    def can_write(self, ctx, namespace: str, name: str) -> bool:  # type: ignore[no-untyped-def]
        return namespace != "channel"


async def test_a2_11_denied_store_blocks_whole_line() -> None:
    r = await run(make_runtime(access=DenyWrites()), "!random 1-6 > channel.x")
    assert (r.result.code, r.send, r.executed) == (126, None, [])


async def test_a2_12_forward_reference_rejected() -> None:
    r = await run(make_runtime(), "!echo {1}")
    assert r.result.code == Code.USAGE and "runs later" in (r.send or "")


# ── operators and stdin ────────────────────────────────────────────────────
async def test_pipe_delivers_stdin_and_and_does_not() -> None:
    rt = make_runtime()
    piped = await run(rt, "!echo hello | upper")
    assert piped.send == "HELLO"
    anded = await run(rt, "!echo hello && upper")
    assert anded.result.ok and anded.send is None  # upper got no stdin → empty message → nothing sent


async def test_input_none_command_rejected_after_pipe() -> None:
    r = await run(make_runtime(), "!echo hi | weather Lisbon")
    assert r.result.code == Code.USAGE and r.send == "weather does not accept piped input"


async def test_pipe_into_group_reaches_first_invocation() -> None:
    r = await run(make_runtime(), "!echo shout | ( upper && echo {_} again )")
    assert r.send == "SHOUT again"


async def test_or_returns_left_when_ok_and_and_returns_left_when_failed() -> None:
    rt = make_runtime()
    assert (await run(rt, "!echo left || echo right")).send == "left"
    r = await run(rt, "!false && echo right")
    assert (r.result.code, r.send) == (1, None)


# ── sentinels ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("expr", "code", "send"),
    [
        ("!fail", 1, None),
        ("!fail 2 max 20 dice", 2, "max 20 dice"),
        ("!fail oops it broke", 1, "oops it broke"),
        ("!default", 2, None),
        ("!true extra", 2, None),
        ("!echo", 0, None),
        ("!weather Lisbon | true", 0, None),
    ],
)
async def test_sentinels(expr: str, code: int, send: str | None) -> None:
    r = await run(make_runtime(), expr)
    assert r.result.code == code
    if code == 0 or send is not None:
        assert r.send == send


async def test_true_passes_data_through() -> None:
    r = await run(make_runtime(), "!weather Lisbon | true | echo {_.celsius}")
    assert r.send == "21.5"


# ── placeholders ───────────────────────────────────────────────────────────
async def test_bare_result_prefers_scalar_data_then_message() -> None:
    rt = make_runtime()
    assert (await run(rt, "!add 2 3 | echo {1}")).send == "5"
    assert (await run(rt, "!weather Lisbon | echo {1}")).send == "Lisbon: 21.5°C"
    assert (await run(rt, "!weather Lisbon | echo {1.tags}")).send == "sun, warm"
    assert (await run(rt, "!weather Lisbon | echo {1.tags.1} {1.code}")).send == "warm 0"


async def test_fallbacks_and_types() -> None:
    rt = make_runtime()
    assert (await run(rt, "!echo {chatter.location ?? {channel.location ?? Lisbon}}")).send == "Lisbon"
    assert (await run(rt, "!add 1 1 | echo {1:int ?? no}")).send == "2"
    assert (await run(rt, "!weather Lisbon | echo {1.nope ?? none}")).send == "none"
    assert (await run(rt, "!echo abc | echo {_:int ?? not a number}")).send == "not a number"


async def test_context_fields() -> None:
    rt = make_runtime()
    r = await run(rt, "!echo {chatter.display} in {channel.name} rank {chatter.rank} sub={chatter.is_sub}")
    assert r.send == "Alice in doomtp rank 20 sub=true"


async def test_placeholder_expansion_is_not_re_lexed() -> None:
    store = InMemoryVariableStore()
    rt = make_runtime(store=store)
    stored = await run(rt, '!echo "a | b && \\{x}" > chatter.trick')
    assert stored.result.ok
    r = await run(rt, "!echo {chatter.trick}")
    assert r.send == "a | b && {x}" and r.executed == [1]


async def test_root_not_available_in_line_context() -> None:
    r = await run(make_runtime(), "!echo {arg.1}")
    assert r.result.code == Code.USAGE and "not available" in (r.send or "")


async def test_arg_captures_in_body_context() -> None:
    rt = make_runtime()
    args = ScopeArgs.from_text('6   for  "initiative"')
    r = await run(rt, "echo {arg.1}|{arg.2+}|{arg.2+raw}|{arg.count}", context=Context.BODY, scope_args=args)
    # from_text is a plain whitespace split (free text like redemption input); quotes stay literal.
    assert r.send == '6|for "initiative"|for  "initiative"|3'


# ── arguments ──────────────────────────────────────────────────────────────
async def test_argument_validation_usage_message() -> None:
    rt = make_runtime()
    r = await run(rt, "!add one 2")
    assert r.result.code == Code.USAGE and r.send == "usage: !add <a> <b> — a: expected a whole number"
    r = await run(rt, "!add 1")
    assert r.send == "usage: !add <a> <b> — b is required"
    r = await run(rt, "!ping extra")
    assert r.send == "usage: !ping — takes no arguments"


async def test_random_uses_context_rng() -> None:
    rt = make_runtime()
    expected = random.Random(7).randint(1, 6)
    r = await run(rt, "!random 1-6 | echo you rolled {1}", seed=7)
    assert r.send == f"you rolled {expected}"


# ── variables ──────────────────────────────────────────────────────────────
async def test_read_your_writes_and_append() -> None:
    store = InMemoryVariableStore()
    rt = make_runtime(store=store)
    r = await run(rt, "!echo a >> chatter.log && echo b >> chatter.log && echo {chatter.log}")
    assert r.send == "a, b"
    assert store.data[VarKey("chatter", "u1", name="log")] == ["a", "b"]


async def test_append_to_non_list_fails_and_nothing_is_committed_for_that_op() -> None:
    store = InMemoryVariableStore()
    rt = make_runtime(store=store)
    await run(rt, "!echo x > chatter.v")
    r = await run(rt, "!echo y >> chatter.v")
    assert r.result.code == Code.USAGE and r.send == "chatter.v is not a list"
    assert store.data[VarKey("chatter", "u1", name="v")] == "x"


async def test_writes_commit_even_when_final_code_fails() -> None:
    store = InMemoryVariableStore()
    r = await run(make_runtime(store=store), "!echo kept > channel.note && false")
    assert r.result.code == 1 and store.data[VarKey("channel", "c1", name="note")] == "kept"


async def test_publisher_namespaces_only_in_custom_commands() -> None:
    rt = make_runtime()
    r = await run(rt, "!echo x > publisher.y")
    assert r.result.code == Code.USAGE and "only available inside custom commands" in (r.send or "")
    pub = Publisher(id="p9", login="bob")
    store = InMemoryVariableStore()
    rt = make_runtime(store=store)
    ok = await run(rt, "echo x > publisher.channel.chatter.score", context=Context.BODY, publisher=pub)
    assert ok.result.ok and store.data[VarKey("publisher.channel.chatter", "p9", "c1", "u1", "score")] == "x"


async def test_chatter_namespace_without_invoker() -> None:
    r = await run(make_runtime(), "echo x > chatter.y", invoker=None, context=Context.TRIGGER)
    assert r.result.code == Code.USAGE and "needs a chatter" in (r.send or "")


# ── failures, limits, cancellation ─────────────────────────────────────────
async def test_stage_timeout() -> None:
    rt = make_runtime()
    rt.executor.stage_timeout = 0.05
    r = await run(rt, "!slow || echo recovered {_.code}")
    assert r.send == "recovered 124"


async def test_expression_timeout_discards_writes() -> None:
    store = InMemoryVariableStore()
    rt = make_runtime(store=store, expr_timeout=0.05)
    r = await run(rt, "!echo x > chatter.t && slow")
    assert r.result.code == Code.TIMEOUT and store.data == {}


async def test_commands_cannot_return_runtime_reserved_codes() -> None:
    r = await run(make_runtime(), "!fakedeny")
    assert (r.result.code, r.send) == (Code.FAIL, "nope")


async def test_crashing_command_is_contained() -> None:
    r = await run(make_runtime(), "!boom || echo still here")
    assert r.send == "still here"


async def test_too_many_invocations() -> None:
    expr = "!" + " | ".join(["echo x"] * 9)
    r = await run(make_runtime(), expr)
    assert r.result.code == Code.USAGE and r.send == "too many commands (max 8)"


async def test_moderation_cancellation_discards_writes() -> None:
    store = InMemoryVariableStore()
    rt = make_runtime(store=store)
    flag = {"cancelled": False}
    r1 = await run(rt, "!echo x > chatter.c && cancelme", is_cancelled=lambda: flag["cancelled"])
    assert r1.send == "not cancelled"
    flag["cancelled"] = True
    store.data.clear()
    r2 = await run(rt, "!echo x > chatter.c && cancelme", is_cancelled=lambda: flag["cancelled"])
    assert (r2.result.code, r2.send, r2.cancelled, store.data) == (130, None, True, {})


async def test_failures_carry_the_specs_error_identifier() -> None:
    rt = make_runtime()
    bad_ref = await run(rt, "!echo {2}")
    assert bad_ref.result.code == Code.USAGE
    assert bad_ref.result.data == {
        "error": "E_BAD_REFERENCE",
        "reference": "{2}",
    }
    missing = await run(rt, "!echo {chatter.unset}")  # no ?? fallback (spec §7.5)
    assert missing.result.data == {"error": "E_MISSING_VALUE", "reference": "{chatter.unset}"}
    too_many = await run(rt, " && ".join(["!echo x"] * 9))
    assert too_many.result.data == {"error": "E_TOO_MANY", "max": 8}
    parse = await run(rt, "!echo a ; b")
    assert isinstance(parse.result.data, dict) and parse.result.data["error"] == "E_RESERVED_OPERATOR"


# ── parse errors and raw tails ─────────────────────────────────────────────
async def test_parse_error_visible_only_when_first_command_runnable() -> None:
    rt = make_runtime(policy=DenyAdd())
    shown = await run(rt, "!echo a ; b")
    assert shown.origin == "parse" and shown.send and "E_RESERVED_OPERATOR" in shown.send
    hidden = await run(rt, "!add 1 ; 2")
    assert hidden.send is None
    unknown = await run(rt, "!nope a ; b")
    assert unknown.send is None


async def test_not_a_command_returns_none() -> None:
    rt = make_runtime()
    ctx = rt.make_context(channel=CHANNEL, invoker=ALICE)
    assert await rt.run("hello chat", ctx) is None


async def test_raw_tail_reaches_handler() -> None:
    r = await run(make_runtime(), "!rawecho !a | b {c}")
    assert r.send == "raw=!a | b {c}"


async def test_stdin_for_body_scope() -> None:
    r = await run(make_runtime(), "upper", context=Context.BODY, stdin=Result.success("hi"))
    assert r.send == "HI"


async def test_reply_mention_is_stripped() -> None:
    r = await run(make_runtime(), "@bob !ping", reply_parent_login="bob")
    assert r.send == "pong"
