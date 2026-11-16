"""Unit tests for the general purpose sensor decoder (general_purpose.py).

Run: pytest tests/  (or: python3 tests/test_general_purpose.py)
"""

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "atios_general_purpose",
    Path(__file__).resolve().parent.parent / "custom_components" / "atios" / "general_purpose.py",
)
gp = importlib.util.module_from_spec(_spec)
sys.modules["atios_general_purpose"] = gp
_spec.loader.exec_module(gp)


def test_decodes_reports_like_the_firmware():
    # lib-dali general_purpose_sensor tests, also used by the web UI
    assert gp.Measurement(10, 126, signed=True).decode_event(897) == 26.0
    assert gp.Measurement(6, 127, signed=True).decode_event(0x2F3) == -1
    assert gp.Measurement(12, 128, signed=True).decode_event(0x27F) == -10300


def test_unsigned_and_scaled():
    assert gp.Measurement(9).decode_event(0x200 | 300) == 300
    assert gp.Measurement(8, 125).decode_event(0x200 | (200 << 1)) == 2.0


def test_ignores_other_events_and_mask():
    m = gp.Measurement(6)
    for event in [0x0F3, 0x1FF, 0x400]:
        assert m.decode_event(event) is None, event
    assert m.decode_event(0x3FF) is None  # all ones: MASK
    assert gp.Measurement(0).decode_event(0x2F3) is None
    assert gp.Measurement(33).decode_event(0x2F3) is None


def test_unit_device_class_and_label():
    temp = gp.Measurement(10, 126, True, quantity_name=26, unit_of_measure=9)
    assert (temp.unit, temp.device_class, temp.label, temp.decimals) == (
        "°C", "temperature", "temperature", 1
    )
    humidity = gp.Measurement(8, quantity_name=29, unit_of_measure=50)
    assert (humidity.unit, humidity.device_class) == ("%", "humidity")
    co2 = gp.Measurement(12, quantity_name=34, unit_of_measure=53)
    assert (co2.unit, co2.device_class) == ("ppm", "carbon_dioxide")
    unknown = gp.Measurement(8)
    assert (unknown.unit, unknown.device_class, unknown.label) == (None, None, "sensor")
    assert gp.Measurement(8, unit_of_measure=50).device_class is None  # % alone


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
