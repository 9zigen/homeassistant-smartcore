"""Reload DALI devices button and the device count sensors."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.atios.button import AtiosReloadDaliButton
from custom_components.atios.nvram import parse_control_devices, parse_input_devices
from custom_components.atios.sensor import AtiosDeviceCountSensor


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _entry(loaded=True):
    hub = SimpleNamespace(
        nvm_loaded=loaded,
        control_devices=parse_control_devices(
            [
                {"address": 0, "name": "Desk"},
                {"address": 1, "name": "Shelf"},
                {"address": 129, "is_group": True, "name": "Office"},
            ]
        ),
        input_devices=parse_input_devices(
            [{"address": 2, "name": "Button", "instances": [{"number": 0, "type": 1}, {"number": 1, "type": 1}]}]
        ),
    )
    return SimpleNamespace(unique_id="sc1", entry_id="e1", runtime_data=hub)


def test_device_counts():
    control = AtiosDeviceCountSensor(_entry(), "control")
    assert control.native_value == 2
    assert control.extra_state_attributes == {"groups": 1, "devices": ["A0 Desk", "A1 Shelf"]}
    inputs = AtiosDeviceCountSensor(_entry(), "input")
    assert inputs.native_value == 1
    assert inputs.extra_state_attributes == {"instances": 2, "devices": ["A2 Button"]}


def test_counts_unknown_without_the_nvram_model():
    assert AtiosDeviceCountSensor(_entry(loaded=False), "control").native_value is None


async def test_reload_button_reloads_the_entry(hass):
    button = AtiosReloadDaliButton(_entry())
    button.hass = hass
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await button.async_press()
    reload.assert_called_once_with("e1")
