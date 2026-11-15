"""Hub input monitor tests against a fake SmartCore (aiohttp test server).

Needs aiohttp (it ships with Home Assistant) and pytest-asyncio; skipped without them.
Run: pytest tests/
"""

import asyncio
import json

import pytest

aiohttp = pytest.importorskip("aiohttp")
pytest_asyncio = pytest.importorskip("pytest_asyncio")
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

pytestmark = pytest.mark.asyncio

from _hub_loader import load_hub  # noqa: E402

hub_module = load_hub()


class FakeSmartCore:
    """Lunatone socket on /, web UI socket on /ws, settings and command endpoints."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.ui_sockets: list[web.WebSocketResponse] = []
        self.lunatone_sockets: list[web.WebSocketResponse] = []
        self.app = web.Application()
        self.app.router.add_get("/", self._lunatone)
        self.app.router.add_get("/ws", self._ui)
        self.app.router.add_get("/settings", self._settings)
        self.app.router.add_post("/cmd/{cmd}", self._cmd)

    async def _socket(self, request, sockets, greeting=None):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sockets.append(ws)
        if greeting:
            await ws.send_str(greeting)
        async for _msg in ws:
            pass
        return ws

    async def _lunatone(self, request):
        return await self._socket(request, self.lunatone_sockets, json.dumps({"type": "info"}))

    async def _ui(self, request):
        return await self._socket(request, self.ui_sockets)

    async def _settings(self, _request):
        return web.json_response({"dali_ip_interface": True})

    async def _cmd(self, request):
        self.commands.append(request.match_info["cmd"])
        return web.Response()

    async def send_ui(self, message: str) -> None:
        for ws in self.ui_sockets:
            await ws.send_str(message)

    async def send_lunatone_frame(self, data: list[int]) -> None:
        message = {
            "type": "daliMonitor",
            "data": {"line": 0, "bits": 24, "externalSource": True, "data": data},
        }
        for ws in self.lunatone_sockets:
            await ws.send_str(json.dumps(message))


async def _until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


@pytest_asyncio.fixture
async def device():
    fake = FakeSmartCore()
    server = TestServer(fake.app)
    await server.start_server()
    session = aiohttp.ClientSession()
    hub = hub_module.AtiosHub(f"127.0.0.1:{server.port}", 0, session)
    builtin: list[tuple[int, int]] = []
    frames: list[list[int]] = []
    hub.add_builtin_input_listener(lambda pin, level: builtin.append((pin, level)))
    hub.add_monitor_listener(lambda frame: frames.append(frame.data))
    await hub.async_start()
    await _until(lambda: fake.ui_sockets and fake.lunatone_sockets)
    try:
        yield fake, hub, builtin, frames
    finally:
        await hub.async_stop()
        await session.close()
        await server.close()


async def test_holds_the_input_monitor_lease(device):
    fake, _hub, _builtin, _frames = device
    await _until(lambda: "input_monitor_start" in fake.commands)


async def test_reports_builtin_input_edges(device):
    fake, _hub, builtin, _frames = device
    await fake.send_ui("IN:3,1")
    await fake.send_ui('{"system_event":true}')
    await fake.send_ui("DALI:[IN],DA24 Evt,88841F,NA")
    await fake.send_ui("IN:3,0")
    await _until(lambda: len(builtin) == 2)
    assert builtin == [(3, 1), (3, 0)]


async def test_dali_stays_on_the_lunatone_socket(device):
    fake, _hub, builtin, frames = device
    await fake.send_ui("DALI2:1,0,1")  # the input monitor's DALI-2 events are ignored
    await fake.send_lunatone_frame([2, 0x80, 1])
    await fake.send_ui("IN:0,1")
    await _until(lambda: builtin and frames)
    await asyncio.sleep(0.2)
    assert frames == [[2, 0x80, 1]]
    assert builtin == [(0, 1)]
