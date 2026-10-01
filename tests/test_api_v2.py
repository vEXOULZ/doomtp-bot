"""/api/v2 (ADR-0027): vex-platform's job and audit routes, behind v1's callers and access rules."""
# ruff: noqa: F811  (the imported fixtures are parameters here, which ruff reads as redefinitions)

from __future__ import annotations

from typing import Any

import httpx
from vex_platform.actor import Actor
from vex_platform.audit import psycopg as audit_pg
from vex_platform.audit.model import AuditEntry

from doomtp_bot.api.keys import ApiKeyService
from doomtp_bot.audit.log import TABLE as AUDIT_TABLE
from doomtp_bot.history.jobs import KIND
from tests.test_api_data import (  # noqa: F401  (fixtures)
    CHANNEL_ID,
    CHANNEL_LOGIN,
    app_and_keys,
    auth,
    client,
    write_key,
)
from tests.test_api_moderator import MOD_ID, OTHER_ID, OTHER_LOGIN, mod  # noqa: F401  (fixtures)

V2 = "/api/v2"


async def queue_range(client: httpx.AsyncClient, key: str, from_ms: int = 1000) -> dict[str, Any]:
    url = f"/api/v1/channels/{CHANNEL_LOGIN}/backfill"
    response = await client.post(url, json={"from_ms": from_ms, "to_ms": 5000}, headers=auth(key))
    assert response.status_code == 201, response.text
    job: dict[str, Any] = response.json()["jobs"][0]
    return job


async def test_errors_are_problem_details_under_v2_only(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{V2}/jobs")
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert (body["status"], body["code"]) == (401, "unauthenticated")
    assert body["request_id"] == response.headers["x-request-id"]
    # v1 answers as it always did.
    assert (await client.get("/api/v1/channels")).json() == {
        "detail": "an API key or an admin session is required"
    }
    assert (await client.get(f"{V2}/openapi.json")).status_code == 401


async def test_an_admin_follows_and_cancels_a_backfill_run(client: httpx.AsyncClient, write_key: str) -> None:
    first = await queue_range(client, write_key)
    second = await queue_range(client, write_key, from_ms=2000)

    page = (await client.get(f"{V2}/jobs", params={"limit": 1}, headers=auth(write_key))).json()
    assert [j["id"] for j in page["items"]] == [second["id"]] and page["next_cursor"]
    rest = await client.get(f"{V2}/jobs", params={"cursor": page["next_cursor"]}, headers=auth(write_key))
    assert [j["id"] for j in rest.json()["items"]] == [first["id"]]

    run = (await client.get(f"{V2}/jobs/{first['id']}", headers=auth(write_key))).json()
    assert (run["kind"], run["subject"], run["state"]) == (KIND, f"channel:{CHANNEL_ID}", "queued")
    assert run["actor"] == {"kind": "api_key", "id": "tests", "login": None, "via": "api"}
    assert run["created_at"].endswith("Z")

    cancelled = await client.post(f"{V2}/jobs/{first['id']}/cancel", headers=auth(write_key))
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    again = await client.post(f"{V2}/jobs/{first['id']}/cancel", headers=auth(write_key))
    assert again.status_code == 409 and again.json()["code"] == "job_conflict"
    missing = await client.get(f"{V2}/jobs/999", headers=auth(write_key))
    assert missing.status_code == 404 and missing.json()["code"] == "job_not_found"

    kinds = (await client.get(f"{V2}/job-kinds", headers=auth(write_key))).json()
    assert [k["name"] for k in kinds] == [KIND] and kinds[0]["cancel_mode"] == "cooperative"

    audit = await client.get(f"{V2}/audit", params={"action": "job."}, headers=auth(write_key))
    rows = [(r["action"], r["job_run_id"], r["actor_kind"], r["scope"]) for r in audit.json()["items"]]
    assert rows == [
        ("job.cancel", first["id"], "api_key", CHANNEL_ID),
        ("job.enqueue", second["id"], "api_key", CHANNEL_ID),
        ("job.enqueue", first["id"], "api_key", CHANNEL_ID),
    ]


async def test_runs_are_queued_through_the_backfill_routes_not_v2(
    client: httpx.AsyncClient, write_key: str
) -> None:
    """The backfill routes check consent and the range; `POST /jobs` would skip both."""
    body = {"kind": KIND, "subject": f"channel:{CHANNEL_ID}", "payload": {}}
    refused = await client.post(f"{V2}/jobs", json=body, headers=auth(write_key))
    assert refused.status_code == 422 and refused.json()["code"] == "invalid_job"


async def test_a_moderator_reads_their_channels_audit_but_not_the_jobs(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], mod: dict[str, str]
) -> None:
    runtime = app_and_keys[0].state.backfill.runtime
    for scope in (CHANNEL_ID, OTHER_ID, None):
        await runtime.enqueue(KIND, f"channel:{scope}", {"channel_id": scope}, scope=scope)

    assert (await client.get(f"{V2}/jobs")).status_code == 403
    denied = await client.post(f"{V2}/jobs/1/cancel", headers=mod)
    assert denied.status_code == 403 and denied.json()["code"] == "forbidden"
    assert (await runtime.get(1)).state == "queued"
    await runtime.cancel(1)

    rows = (await client.get(f"{V2}/audit")).json()["items"]
    # Their channel's rows, the cancel too, and their own refusal (no channel): not another channel's, nor
    # a global one.
    assert [(r["action"], r["scope"]) for r in rows] == [
        ("job.cancel", CHANNEL_ID),
        ("request.denied", None),
        ("job.enqueue", CHANNEL_ID),
    ]
    assert (await client.get(f"{V2}/openapi.json")).status_code == 200


async def test_audit_finds_actors_and_names_channels_and_logins(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], write_key: str, mod: dict[str, str]
) -> None:
    """`actor=me` or a login (found through Twitch), and v1's `channel_login` and `actor_login` as
    `scope_name` and a filled-in `actor_login`."""
    pool = app_and_keys[0].state.backfill.runtime.pool
    target = "command:v2-audit-test"  # committed on the runtime's pool: deleted again below

    async def rows(headers: dict[str, str], **params: str) -> list[tuple[Any, ...]]:
        response = await client.get(f"{V2}/audit", params={"target": target, **params}, headers=headers)
        assert response.status_code == 200, response.text
        return [(r["actor_id"], r["actor_login"], r["scope_name"]) for r in response.json()["items"]]

    async with pool.connection() as conn:
        for actor, scope in [
            (Actor("user", "200", None, "chat"), CHANNEL_ID),  # friend, recorded by id only
            (Actor("user", "400", "alice", "web"), OTHER_ID),
            (Actor("user", MOD_ID, "mod", "web"), OTHER_ID),
        ]:
            await audit_pg.record(conn, AuditEntry("cc.edit", actor, target, scope=scope), table=AUDIT_TABLE)
    try:
        admin = auth(write_key)
        assert await rows(admin, actor="friend") == [("200", "friend", CHANNEL_LOGIN)]
        assert await rows(admin, actor="ALICE") == [("400", "alice", OTHER_LOGIN)]
        assert await rows(admin, actor="nobody") == []
        assert await rows(admin, actor="me") == []  # the key wrote none of them
        # The moderator: their channel's row and their own, not alice's in the other channel.
        assert await rows({}) == [(MOD_ID, "mod", OTHER_LOGIN), ("200", "friend", CHANNEL_LOGIN)]
        assert await rows({}, actor="me") == [(MOD_ID, "mod", OTHER_LOGIN)]
        assert await rows({}, actor="alice") == []
    finally:
        async with pool.connection() as conn:
            await conn.execute(f"DELETE FROM {AUDIT_TABLE} WHERE target = %s", (target,))


async def test_a_refused_write_is_audited_as_the_caller(
    client: httpx.AsyncClient, app_and_keys: tuple[Any, ApiKeyService], write_key: str, mod: dict[str, str]
) -> None:
    assert (await client.post(f"{V2}/jobs/1/pause", headers=mod)).status_code == 403
    client.cookies.clear()
    rows = (await client.get(f"{V2}/audit", params={"action": "request."}, headers=auth(write_key))).json()
    (row,) = rows["items"]
    assert (row["action"], row["outcome"], row["actor_kind"], row["actor_id"], row["via"]) == (
        "request.denied",
        "denied",
        "user",
        MOD_ID,
        "web",
    )
    assert row["detail"] == {"method": "POST", "path": f"{V2}/jobs/1/pause", "status": 403}
    assert row["request_id"]


async def test_the_openapi_lists_the_v2_routes(client: httpx.AsyncClient, write_key: str) -> None:
    schema = (await client.get(f"{V2}/openapi.json", headers=auth(write_key))).json()
    assert {f"{V2}/jobs", f"{V2}/jobs/{{run_id}}/events", f"{V2}/audit"} <= set(schema["paths"])
    docs = await client.get(f"{V2}/docs", headers=auth(write_key))
    assert docs.status_code == 200 and "swagger" in docs.text.lower()
