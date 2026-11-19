"""Unit tests for the light state mirror (gear_state.py).

Run: pytest tests/  (or: python3 tests/test_gear_state.py)
"""

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "atios_gear_state",
    Path(__file__).resolve().parent.parent / "custom_components" / "atios" / "gear_state.py",
)
gs = importlib.util.module_from_spec(_spec)
sys.modules["atios_gear_state"] = gs
_spec.loader.exec_module(gs)


def _mirror(**levels):
    gears = [
        gs.Gear(0, groups={1}, scenes={0: 100, 1: gs.MASK}),
        gs.Gear(1, groups={1, 2}, min_level=10, max_level=200),
        gs.Gear(2),
    ]
    for gear in gears:
        if (level := levels.get(f"a{gear.address}")) is not None:
            gear.set_level(level)
    return gs.BusMirror(gears)


def test_dapc_short_group_broadcast():
    m = _mirror()
    assert m.apply([0x00, 120], 16).changed == {0}  # A0 DAPC 120
    assert m.level(0) == 120
    assert m.apply([0x82, 5], 16).changed == {0, 1}  # group 1 DAPC 5
    assert (m.level(0), m.level(1)) == (5, 10)  # gear 1 clamps to its min level
    assert m.apply([0xFE, 0], 16).changed == {0, 1, 2}  # broadcast off
    assert m.all_level() == 0


def test_dapc_mask_is_ignored():
    m = _mirror(a0=50)
    assert not m.apply([0x00, 0xFF], 16).changed
    assert m.level(0) == 50


def test_off_recall_and_last_active():
    m = _mirror(a1=150)
    m.apply([0x03, gs.OFF], 16)
    assert m.level(1) == 0
    m.apply([0x03, gs.GO_TO_LAST_ACTIVE_LEVEL], 16)
    assert m.level(1) == 150
    m.apply([0x03, gs.RECALL_MAX], 16)
    assert m.level(1) == 200
    m.apply([0x03, gs.RECALL_MIN], 16)
    assert m.level(1) == 10


def test_unknown_results_ask_for_a_query():
    m = _mirror()
    assert m.apply([0x05, gs.GO_TO_LAST_ACTIVE_LEVEL], 16).query == {2}
    assert m.apply([0x05, gs.STEP_UP], 16).query == {2}
    assert m.apply([0xFF, gs.CONTINUOUS_UP], 16).query == {0, 1, 2}


def test_scenes_from_nvram():
    m = _mirror(a0=0, a1=30)
    update = m.apply([0xFF, gs.GO_TO_SCENE], 16)  # broadcast scene 0
    assert m.level(0) == 100
    assert update.query == {1, 2}  # no scene levels known for them
    m.apply([0xFF, gs.GO_TO_SCENE + 1], 16)  # gear 0 is not in scene 1 (MASK)
    assert m.level(0) == 100


def test_up_down_use_the_fade_rate_like_the_firmware():
    assert gs.fade_step(7) == 10  # 50 steps/s for 200 ms
    m = _mirror(a2=100)
    m.apply([0x05, gs.UP], 16)
    assert m.level(2) == 110
    m.apply([0x05, gs.DOWN], 16)
    assert m.level(2) == 100
    m.apply([0x05, gs.OFF], 16)
    m.apply([0x05, gs.UP], 16)  # UP never switches a gear on
    assert m.level(2) == 0


def test_steps():
    m = _mirror(a1=10)
    m.apply([0x03, gs.STEP_DOWN_AND_OFF], 16)
    assert m.level(1) == 0
    m.apply([0x03, gs.ON_AND_STEP_UP], 16)
    assert m.level(1) == 10
    m.apply([0x03, gs.STEP_UP], 16)
    assert m.level(1) == 11


def test_queried_level_from_the_bus():
    m = _mirror()
    assert not m.apply([0x01, 0xA0], 16)  # someone queries A0
    assert m.apply([77], 8).changed == {0}
    assert m.level(0) == 77
    m.apply([0x01, 0xA0], 16)
    m.apply([0x00, 3], 16)  # another frame first: the answer is not ours to take
    assert not m.apply([99], 8).changed


def test_groups_and_limits_follow_configuration():
    m = _mirror(a2=200)
    m.apply([0x05, gs.ADD_TO_GROUP + 3], 16)
    assert [g.address for g in m.members(3)] == [2]
    m.apply([0xA3, 150], 16)  # DTR0
    m.apply([0x05, gs.SET_MAX_LEVEL], 16)
    assert m.level(2) == 150
    m.apply([0x05, gs.REMOVE_FROM_GROUP + 3], 16)
    assert m.members(3) == []


def test_group_level_and_groups_without_members():
    m = _mirror(a0=0, a1=80)
    assert m.group_level(1) == 80  # highest member
    assert m.group_level(9) is None
    update = m.apply([0x93, gs.RECALL_MAX], 16)  # group 9 has no known members
    assert update.groups == {9}
    assert m.group_level(9) == 254


def test_ignores_input_device_frames():
    m = _mirror(a0=1)
    assert not m.apply([0x00, 0x80, 0x01], 24)
    assert m.level(0) == 1


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
