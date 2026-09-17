import httpx

from doomtp_bot.api.app import create_app
from doomtp_bot.core.health import ComponentHealth, HealthRegistry, Status


def _client(registry: HealthRegistry) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(registry)), base_url="http://test")


async def test_healthz_is_ok_without_components() -> None:
    async with _client(HealthRegistry()) as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}


async def test_readyz_reports_components_and_503_when_unhealthy() -> None:
    registry = HealthRegistry()

    async def ok() -> ComponentHealth:
        return ComponentHealth(Status.OK, {"schema": 1})

    async def broken() -> ComponentHealth:
        raise RuntimeError("boom")

    registry.register("databases", ok)
    registry.register("twitch", broken)
    async with _client(registry) as client:
        resp = await client.get("/readyz")
    body = resp.json()
    assert resp.status_code == 503
    assert body["status"] == "unhealthy"
    assert body["components"]["databases"] == {"status": "ok", "schema": 1}
    assert body["components"]["twitch"]["status"] == "unhealthy"


async def test_readyz_disabled_component_does_not_fail() -> None:
    registry = HealthRegistry()

    async def disabled() -> ComponentHealth:
        return ComponentHealth(Status.DISABLED, {"reason": "not configured"})

    registry.register("twitch", disabled)
    async with _client(registry) as client:
        resp = await client.get("/readyz")
    assert resp.status_code == 200 and resp.json()["status"] == "ok"
