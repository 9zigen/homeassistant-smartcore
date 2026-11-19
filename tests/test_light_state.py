"""Light state tracking: one query per gear at start, then the bus decides.

Runs on pytest-homeassistant-custom-component with a fake hub; no hardware.
"""

from types import SimpleNamespace

import pytest

from custom_components.atios import light
from custom_components.atios.dali import Target
from custom_components.atios.hub import MonitorFrame
from custom_components.atios.nvram import parse_control_devices


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


class FakeHub:
    host = "192.0.2.1"
    serial = "SC-1"
    sw_version = "3.2.64"
    available = True
    bus_scan_active = False

    def __init__(self, levels: dict[int, int], connected: bool = True) -> None:
        self.levels = levels
        self.dali_ip_connected = connected
        self.sent: list[list[int]] = []
        self.bus_cbs = []
        self.connected_cbs = []
        self.control_devices = parse_control_devices(
            [
                {"address": 0, "name": "Desk", "gear_groups": [1],
                 "gear_scenes": [{"number": 2, "enabled": True, "level": 120}]},
                {"address": 1, "name": "Shelf", "gear_groups": [1]},
                {"address": 129, "is_group": True, "name": "Office"},
            ]
        )

    async def send_frame(self, frame):
        self.sent.append(list(frame.data))
        if frame.wait_for_answer:
            level = self.levels.get(frame.data[0] >> 1)
            return None if level is None else [level]
        return None

    def bus(self, data):
        for cb in list(self.bus_cbs):
            cb(MonitorFrame(data=data, bits=len(data) * 8, external=False))

    def add_bus_listener(self, cb):
        self.bus_cbs.append(cb)
        return lambda: self.bus_cbs.remove(cb)

    def add_connected_listener(self, cb):
        self.connected_cbs.append(cb)
        return lambda: self.connected_cbs.remove(cb)

    def add_bus_scan_listener(self, cb):
        return lambda: None

    def add_ip_interface_listener(self, cb):
        return lambda: None

    def add_available_listener(self, cb):
        return lambda: None


def _entities(hass, hub):
    entry = SimpleNamespace(unique_id="sc1", options={})
    tracker = light.LightStateTracker(hass, hub, light._gears(hub, []))
    lights = {
        dev.name: light.AtiosLight(hub, entry, tracker, dev.target, name=dev.name, device=dev)
        for dev in hub.control_devices
    }
    return tracker, lights


async def test_each_gear_is_queried_once_then_follows_the_bus(hass):
    hub = FakeHub({0: 254, 1: 0})
    tracker, lights = _entities(hass, hub)
    tracker.start()
    await hass.async_block_till_done()
    assert hub.sent == [[1, 0xA0], [3, 0xA0]]
    desk, shelf, office = lights["Desk"], lights["Shelf"], lights["Office"]
    for entity in lights.values():
        entity._read_state()
    assert (desk.is_on, desk.brightness, shelf.is_on) == (True, 255, False)
    assert office.is_on and not office.assumed_state  # brightest member

    hub.bus([0x82, 0])  # someone switches group 1 off with DAPC 0
    for entity in lights.values():
        entity._read_state()
    assert not desk.is_on and not office.is_on

    hub.bus([0x01, 0x12])  # GO TO SCENE 2: level from the NVRAM scene table
    desk._read_state()
    assert desk.brightness == light.dali_level_to_ha_brightness(120)
    await hass.async_block_till_done()
    assert hub.sent == [[1, 0xA0], [3, 0xA0]]  # no polling, no extra query


async def test_unknown_results_are_queried_later(hass, monkeypatch):
    monkeypatch.setattr(light, "QUERY_AFTER_S", 0)
    hub = FakeHub({0: 10, 1: 30})
    tracker, lights = _entities(hass, hub)
    tracker.start()
    await hass.async_block_till_done()
    hub.sent.clear()
    hub.levels[1] = 200
    hub.bus([0x03, 0x12])  # Shelf has no level stored for scene 2
    await hass.async_block_till_done()
    assert hub.sent == [[3, 0xA0]]
    lights["Shelf"]._read_state()
    assert lights["Shelf"].brightness == light.dali_level_to_ha_brightness(200)


async def test_reconnect_reads_again(hass):
    hub = FakeHub({0: 5, 1: 5}, connected=False)
    tracker, _ = _entities(hass, hub)
    tracker.start()
    await hass.async_block_till_done()
    assert hub.sent == []  # waits for the DALI IP Interface socket
    for cb in hub.connected_cbs:
        cb()
    await hass.async_block_till_done()
    assert hub.sent == [[1, 0xA0], [3, 0xA0]]


async def test_turn_on_sends_and_the_bus_echo_sets_the_state(hass):
    hub = FakeHub({0: 0, 1: 0})
    tracker, lights = _entities(hass, hub)
    tracker.start()
    await hass.async_block_till_done()
    desk = lights["Desk"]
    desk.hass = hass
    desk.entity_id = "light.desk"
    await desk.async_turn_on(brightness=255)
    assert hub.sent[-1] == [0, 254]
    hub.bus([0, 254])  # the echo of our own frame
    desk._read_state()
    assert desk.is_on and desk.brightness == 255
    assert Target.short(0) == desk._target
