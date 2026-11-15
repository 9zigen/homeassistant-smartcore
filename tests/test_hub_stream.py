"""Hub tests for the DALI IP Interface stream watchdog, query answers and availability.

Against a fake SmartCore (aiohttp test server). Needs aiohttp (it ships with
Home Assistant) and pytest-asyncio; skipped without them.
Run: pytest tests/
"""

import asyncio
import json

import pytest

aiohttp = pytest.importorskip("aiohttp")
pytest_asyncio = pytest.importorskip("pytest_asyncio")
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from _hub_loader import load_hub  # noqa: E402

pytestmark = pytest.mark.asyncio

hub_module = load_hub()
dali = __import__("atios_pkg.dali", fromlist=["Target"])


class FakeSmartCore:
    """DALI IP Interface socket that greets but echoes nothing on its own, plus /api/dali/iface."""

    def __init__(self) -> None:
        self.dali_ip: list[web.WebSocketResponse] = []
        self.handshakes = 0
        self.iface_status = 200
        self.iface_frames: list[list[int]] = []
        self.iface_body = {"success": True, "bus_busy": False, "collision_detected": False, "data": 200}
        self.app = web.Application()
        self.app.router.add_get("/", self._dali_ip)
        self.app.router.add_get("/ws", self._ui)
        self.app.router.add_get("/settings", self._settings)
        self.app.router.add_post("/cmd/{cmd}", self._ok)
        self.app.router.add_post("/api/dali/iface", self._iface)

    async def _dali_ip(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.handshakes += 1
        self.dali_ip.append(ws)
        await ws.send_str(json.dumps({"type": "info"}))
        async for _msg in ws:
            pass
        return ws

    async def _ui(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for _msg in ws:
            pass
        return ws

    async def _settings(self, _request):
        return web.json_response({"dali_ip_interface": True})

    async def _ok(self, _request):
        return web.Response()

    async def _iface(self, request):
        self.iface_frames.append((await request.json())["data"])
        if self.iface_status != 200:
            return web.Response(status=self.iface_status)
        return web.json_response(self.iface_body)

    async def echo(self, data: list[int], counter: int = 1) -> None:
        message = {
            "type": "daliMonitor",
            "data": {"line": 0, "bits": len(data) * 8, "externalSource": False, "data": data,
                     "timestamp": 12.5},
            "timeSignature": {"timestamp": 12.6, "counter": counter},
        }
        for ws in self.dali_ip:
            if not ws.closed:
                await ws.send_str(json.dumps(message))


async def _until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


@pytest_asyncio.fixture
async def device(monkeypatch):
    monkeypatch.setattr(hub_module, "DALI_IP_ECHO_TIMEOUT_S", 0.3)
    monkeypatch.setattr(hub_module, "DALI_IP_ECHO_CHECK_S", 0.05)
    monkeypatch.setattr(hub_module, "RECONNECT_MIN", 0.05)
    fake = FakeSmartCore()
    server = TestServer(fake.app)
    await server.start_server()
    session = aiohttp.ClientSession()
    hub = hub_module.AtiosHub(f"127.0.0.1:{server.port}", 0, session)
    await hub.async_start()
    await _until(lambda: fake.handshakes == 1 and hub.stream_ok)
    await _until(lambda: hub.ip_interface_enabled is True)
    try:
        yield fake, hub
    finally:
        await hub.async_stop()
        await session.close()
        await server.close()


QUERY = dali.query_actual_level(dali.Target.short(3))


async def test_a_missing_echo_marks_the_stream_lost_and_reconnects(device):
    fake, hub = device
    lost: list[bool] = []
    hub.add_stream_lost_listener(lost.append)

    assert await hub.send_frame(QUERY) == [200]
    await _until(lambda: hub.stream_lost)
    await _until(lambda: fake.handshakes == 2)

    await fake.echo([7, 160])
    await _until(lambda: not hub.stream_lost)
    assert lost == [True, False]


async def test_an_echo_keeps_the_stream(device):
    fake, hub = device
    assert await hub.send_frame(QUERY) == [200]
    await fake.echo([7, 160])
    await asyncio.sleep(0.5)
    assert not hub.stream_lost
    assert fake.handshakes == 1


async def test_commands_and_unanswered_queries_do_not_arm_the_watchdog(device):
    fake, hub = device
    await hub.send_frame(dali.off(dali.Target.short(3)))
    fake.iface_body = {"success": False, "bus_busy": False, "collision_detected": False, "data": 0}
    assert await hub.send_frame(QUERY) is None  # "data": 0 without success is no answer
    await asyncio.sleep(0.5)
    assert not hub.stream_lost


async def test_frames_carry_the_device_time_and_counter(device):
    fake, hub = device
    frames = []
    hub.add_monitor_listener(frames.append)
    message = {
        "type": "daliMonitor",
        "data": {"line": 0, "bits": 24, "externalSource": True, "data": [2, 128, 1],
                 "timestamp": 101.25},
        "timeSignature": {"timestamp": 101.3, "counter": 42},
    }
    await fake.dali_ip[0].send_str(json.dumps(message))
    await _until(lambda: frames)
    assert frames[0].device_timestamp == 101.25
    assert frames[0].counter == 42


async def test_lights_go_unavailable_after_repeated_failures_and_come_back(device):
    fake, hub = device
    changes: list[bool] = []
    hub.add_available_listener(changes.append)

    fake.iface_status = 500
    for _ in range(2):
        await hub.send_frame(QUERY)
    assert hub.available
    await hub.send_frame(QUERY)
    assert not hub.available

    fake.iface_status = 200
    await hub.send_frame(QUERY)
    assert hub.available
    assert changes == [False, True]


async def test_a_bus_scan_pauses_dali_requests(device):
    fake, hub = device
    changes: list[bool] = []
    hub.add_bus_scan_listener(changes.append)

    await fake.echo([0xFF, 0xFE, 0x1D])  # START QUIESCENT MODE, sent by the web UI scan
    await _until(lambda: hub.bus_scan_active)
    assert await hub.send_frame(QUERY) is None
    await hub.send_frame(dali.off(dali.Target.short(3)))
    assert fake.iface_frames == []

    # quiescent mode frames still go out, so a stuck scan can be ended
    stop = dali.Frame(data=[0xFF, 0xFE, 0x1E], bits=24)
    await hub.send_frame(stop)
    assert fake.iface_frames == [[0xFF, 0xFE, 0x1E]]

    await fake.echo([0xFF, 0xFE, 0x1E])
    await _until(lambda: not hub.bus_scan_active)
    assert await hub.send_frame(QUERY) == [200]
    assert changes == [True, False]


async def test_a_bus_scan_ends_by_itself_without_stop(device, monkeypatch):
    fake, hub = device
    monkeypatch.setattr(hub_module, "QUIESCENT_MODE_TIMEOUT_S", 0.2)
    await fake.echo([0xFF, 0xFE, 0x1D])
    await _until(lambda: hub.bus_scan_active)
    await _until(lambda: not hub.bus_scan_active)
