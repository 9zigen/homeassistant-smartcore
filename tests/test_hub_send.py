"""Hub DALI request tests against a fake SmartCore (aiohttp test server).

Needs aiohttp (it ships with Home Assistant) and pytest-asyncio; skipped without them.
Run: pytest tests/
"""

import asyncio

import pytest

aiohttp = pytest.importorskip("aiohttp")
pytest_asyncio = pytest.importorskip("pytest_asyncio")
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from _hub_loader import load_hub  # noqa: E402

pytestmark = pytest.mark.asyncio

hub_module = load_hub()
dali = __import__("atios_pkg.dali", fromlist=["Target"])


class FakeIface:
    """/api/dali/iface that answers slowly, like a query waiting for the bus."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0
        self.frames: list[list[int]] = []
        self.app = web.Application()
        self.app.router.add_post("/api/dali/iface", self._iface)

    async def _iface(self, request):
        body = await request.json()
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.05)
        self.frames.append(body["data"])
        self.in_flight -= 1
        answer = 254 if body["wait_response"] else None
        return web.json_response(
            {"success": True, "bus_busy": False, "collision_detected": False, "data": answer}
        )


@pytest_asyncio.fixture
async def device():
    fake = FakeIface()
    server = TestServer(fake.app)
    await server.start_server()
    session = aiohttp.ClientSession()
    hub = hub_module.AtiosHub(f"127.0.0.1:{server.port}", 0, session)
    try:
        yield fake, hub
    finally:
        await session.close()
        await server.close()


async def test_dali_requests_reach_the_device_one_at_a_time(device):
    fake, hub = device
    queries = [dali.query_actual_level(dali.Target.short(address)) for address in range(8)]
    answers = await asyncio.gather(*(hub.send_frame(frame) for frame in queries))

    assert fake.max_in_flight == 1
    assert answers == [[254]] * 8
    assert sorted(fake.frames) == sorted(frame.data for frame in queries)


async def test_commands_keep_their_order(device):
    fake, hub = device
    frames = [dali.level(dali.Target.short(1), value) for value in (10, 20, 30)]
    results = await asyncio.gather(*(hub.send_frame(frame) for frame in frames))

    assert results == [None, None, None]
    assert fake.frames == [frame.data for frame in frames]
