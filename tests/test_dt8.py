"""DT8 colour frame builders (IEC 62386-209) and NVRAM colour capabilities."""

import sys
import types
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
for _name, _path in (
    ("custom_components", _root / "custom_components"),
    ("custom_components.atios", _root / "custom_components" / "atios"),
):
    if _name not in sys.modules:
        _pkg = types.ModuleType(_name)
        _pkg.__path__ = [str(_path)]
        sys.modules[_name] = _pkg

import pytest  # noqa: E402

from custom_components.atios.dali import (  # noqa: E402
    Target,
    dt8_activate,
    dt8_set_colour_temperature,
    dt8_set_xy,
)
from custom_components.atios.nvram import parse_control_devices  # noqa: E402


def _bytes(frames):
    return [tuple(f.data) for f in frames]


def test_set_colour_temperature_short_address():
    # 4000 K = 250 mireds = 0x00FA: DTR0=FA, DTR1=00, enable DT8, 0xE7 to A0
    frames = dt8_set_colour_temperature(Target.short(0), 250)
    assert _bytes(frames) == [(0xA3, 0xFA), (0xC3, 0x00), (0xC1, 0x08), (0x01, 0xE7)]
    assert all(not f.send_twice and not f.wait_for_answer for f in frames)


def test_set_colour_temperature_group():
    # group 1 command address byte: 0x80 | (1 << 1) | 1 = 0x83
    frames = dt8_set_colour_temperature(Target.group(1), 0x01C2)  # 450 mireds
    assert _bytes(frames) == [(0xA3, 0xC2), (0xC3, 0x01), (0xC1, 0x08), (0x83, 0xE7)]


def test_set_colour_temperature_range():
    for bad in (0, 0xFFFF, -1):
        with pytest.raises(ValueError):
            dt8_set_colour_temperature(Target.short(0), bad)


def test_set_xy():
    # x=0.3127 -> 20493 = 0x500D, y=0.3290 -> 21561 = 0x5439 (D65 white)
    frames = dt8_set_xy(Target.short(2), 0.3127, 0.3290)
    assert _bytes(frames) == [
        (0xA3, 0x0D), (0xC3, 0x50), (0xC1, 0x08), (0x05, 0xE0),
        (0xA3, 0x39), (0xC3, 0x54), (0xC1, 0x08), (0x05, 0xE1),
    ]


def test_set_xy_clamps():
    frames = dt8_set_xy(Target.short(0), -0.5, 1.5)
    assert _bytes(frames)[0:2] == [(0xA3, 0x00), (0xC3, 0x00)]
    assert _bytes(frames)[4:6] == [(0xA3, 0xFF), (0xC3, 0xFF)]


def test_activate():
    assert _bytes(dt8_activate(Target.broadcast())) == [(0xC1, 0x08), (0xFF, 0xE2)]


def test_nvram_colour_capabilities():
    devices = parse_control_devices(
        [
            {
                "name": "CCT spot",
                "address": 3,
                "device_types": [6, 8],
                "device_type_selected": 8,
                "color_type_features": 0b10,  # Tc only
                "color_temperature_physical_warmest": 500,
                "color_temperature_physical_coolest": 142,
            },
            {
                "name": "RGB strip",
                "address": 4,
                "device_types": [8],
                "color_type_features": 109,  # bit0 set -> xy, bit1 clear
            },
            {"name": "Plain dimmer", "address": 5, "device_types": [6]},
        ]
    )
    cct, rgb, plain = devices
    assert cct.supports_dt8 and cct.tc_capable and not cct.xy_capable
    assert (cct.ct_warmest_mired, cct.ct_coolest_mired) == (500, 142)
    assert rgb.supports_dt8 and rgb.xy_capable and not rgb.tc_capable
    assert rgb.ct_warmest_mired is None
    assert not plain.supports_dt8 and not plain.tc_capable and not plain.xy_capable


def test_nvram_colour_limits_ignore_zero_and_mask():
    (dev,) = parse_control_devices(
        [
            {
                "name": "x",
                "address": 0,
                "device_types": [8],
                "color_type_features": 2,
                "color_temperature_physical_warmest": 0,
                "color_temperature_physical_coolest": 0xFFFF,
            }
        ]
    )
    assert dev.ct_warmest_mired is None
    assert dev.ct_coolest_mired is None
