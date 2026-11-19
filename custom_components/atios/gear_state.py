"""Light state mirror: follow the DALI bus instead of polling the gear.

Pure module, no Home Assistant or aiohttp imports, unit tested standalone
(tests/test_gear_state.py).

Every forward frame on the bus reaches us as ``daliMonitor`` on the DALI IP
Interface socket: our own commands, the SmartCore's (Matter, accessories,
scenes) and those of other controllers. Each one is applied to the levels of
the gear it addresses, with the same rules as the SmartCore firmware
application layer (lib-dali ``dali16_application.c``): arc power, OFF, RECALL,
UP / DOWN with the fade rate, STEP, plus group membership and min / max level
changes. Commands whose result the frame alone does not tell (a scene the
NVRAM model has no level for, GO TO LAST ACTIVE LEVEL without a known level,
CONTINUOUS UP / DOWN, RESET) ask for a QUERY ACTUAL LEVEL instead, like the
firmware schedules a poll.

Levels are DALI arc power levels 0..254; None is not known yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MASK = 0xFF
MAX_LEVEL = 254

# Address byte forms (first byte of a 16-bit forward frame)
_GROUP_FIRST = 0x80
_GROUP_LAST = 0x9F
_BROADCAST = frozenset({0xFC, 0xFD, 0xFE, 0xFF})  # incl. broadcast unaddressed
_SPECIAL_DTR0 = 0xA3
_QUERY_ACTUAL_LEVEL = 0xA0

# Standard commands (second byte, S bit set)
OFF = 0x00
UP = 0x01
DOWN = 0x02
STEP_UP = 0x03
STEP_DOWN = 0x04
RECALL_MAX = 0x05
RECALL_MIN = 0x06
STEP_DOWN_AND_OFF = 0x07
ON_AND_STEP_UP = 0x08
GO_TO_LAST_ACTIVE_LEVEL = 0x0A
CONTINUOUS_UP = 0x0B
CONTINUOUS_DOWN = 0x0C
GO_TO_SCENE = 0x10  # 0x10..0x1F
RESET = 0x20
SET_MAX_LEVEL = 0x2A
SET_MIN_LEVEL = 0x2B
SET_FADE_RATE = 0x2F
ADD_TO_GROUP = 0x60  # 0x60..0x6F
REMOVE_FROM_GROUP = 0x70  # 0x70..0x7F

# Fade rate steps per second, as the firmware (dali16_standard_api.c fade_rate_table)
_FADE_RATE_TABLE = (
    365.0, 260.0, 185.0, 130.0, 95.0, 70.0, 50.0, 35.0, 25.0, 20.0, 15.0, 14.5, 10.0, 10.0, 10.0,
)


def fade_step(fade_rate: int) -> int:
    """Levels an UP / DOWN moves: the 200 ms fade at the fade rate, like the firmware."""
    index = (fade_rate - 1) & 0xFF
    return int(_FADE_RATE_TABLE[min(index, 14)] * 0.2)


@dataclass
class Gear:
    """One control gear (short address) and what we know about its state."""

    address: int
    groups: set[int] = field(default_factory=set)
    min_level: int = 1
    max_level: int = MAX_LEVEL
    physical_minimum: int = 1
    fade_rate: int = 7
    # scene number -> level, MASK (gear not in the scene) included; missing: unknown
    scenes: dict[int, int] = field(default_factory=dict)
    level: int | None = None
    last_active: int | None = None

    def set_level(self, value: int) -> None:
        self.level = value
        if value > 0:
            self.last_active = value


@dataclass
class Update:
    """What one frame changed: addresses with a new level, addresses to query."""

    changed: set[int] = field(default_factory=set)
    query: set[int] = field(default_factory=set)
    # group numbers / broadcast addressed by a frame, for targets without known members
    groups: set[int] = field(default_factory=set)
    broadcast: bool = False

    def __bool__(self) -> bool:
        return bool(self.changed or self.query or self.groups or self.broadcast)


class BusMirror:
    """Levels of all known gear, kept current from the forward frames on the bus."""

    def __init__(self, gears: list[Gear]) -> None:
        self.gears: dict[int, Gear] = {g.address: g for g in gears}
        self._dtr0: int | None = None
        # Short address of the QUERY ACTUAL LEVEL just sent, its answer is the next 8-bit frame
        self._query_address: int | None = None
        # Last level commanded to a group / broadcast, for targets without known members
        self.group_levels: dict[int, int] = {}
        self.broadcast_level: int | None = None

    # ---- reading -------------------------------------------------------

    def level(self, address: int) -> int | None:
        gear = self.gears.get(address)
        return gear.level if gear else None

    def members(self, group: int) -> list[Gear]:
        return [g for g in self.gears.values() if group in g.groups]

    def group_level(self, group: int) -> int | None:
        """The highest level of the group's members, or the last level sent to the group."""
        members = self.members(group)
        if not members:
            return self.group_levels.get(group)
        return _highest([g.level for g in members])

    def all_level(self) -> int | None:
        if not self.gears:
            return self.broadcast_level
        return _highest([g.level for g in self.gears.values()])

    # ---- writing -------------------------------------------------------

    def set_level(self, address: int, value: int) -> bool:
        """A queried actual level; False when the gear is not known or MASK was read."""
        gear = self.gears.get(address)
        if gear is None or not 0 <= value <= MAX_LEVEL:
            return False
        gear.set_level(value)
        return True

    def apply(self, data: list[int], bits: int) -> Update:
        """Apply one frame seen on the bus (ours or not)."""
        if bits == 8 and len(data) == 1:
            return self._apply_answer(data[0])
        self._query_address = None
        if bits != 16 or len(data) != 2:
            return Update()  # 24-bit frames: input devices, not gear
        first, second = data
        if first <= _GROUP_LAST or first in _BROADCAST:
            return self._apply_addressed(first, second)
        if first == _SPECIAL_DTR0:
            self._dtr0 = second
        return Update()

    def _apply_answer(self, value: int) -> Update:
        address, self._query_address = self._query_address, None
        if address is not None and self.set_level(address, value):
            return Update(changed={address})
        return Update()

    def _apply_addressed(self, first: int, second: int) -> Update:
        update = Update()
        if first in _BROADCAST:
            targets = list(self.gears.values())
            update.broadcast = True
        elif first >= _GROUP_FIRST:
            group = (first >> 1) & 0x0F
            targets = self.members(group)
            update.groups.add(group)
        else:
            gear = self.gears.get(first >> 1)
            targets = [gear] if gear else []

        is_command = bool(first & 1)
        if is_command and second == _QUERY_ACTUAL_LEVEL and first < _GROUP_FIRST:
            self._query_address = first >> 1
            return Update()

        for gear in targets:
            if is_command:
                self._command(gear, second, update)
            elif second != MASK:
                gear.set_level(_clamp(gear, second))
                update.changed.add(gear.address)

        # Remember what a group / broadcast was told, for targets without members
        target_level = None if is_command else second
        if is_command:
            target_level = {OFF: 0, RECALL_MAX: MAX_LEVEL, RECALL_MIN: 1}.get(second)
        if target_level is not None and target_level != MASK:
            if update.broadcast:
                self.broadcast_level = target_level
                self.group_levels = {g: target_level for g in self.group_levels}
            for group in update.groups:
                self.group_levels[group] = target_level
        return update

    def _command(self, gear: Gear, opcode: int, update: Update) -> None:
        level = gear.level
        new: int | None = None
        if opcode == OFF:
            new = 0
        elif opcode in (UP, DOWN):
            if level is None:
                update.query.add(gear.address)
            elif opcode == UP and 0 < level < gear.max_level:
                new = min(level + fade_step(gear.fade_rate), gear.max_level)
            elif opcode == DOWN and level > gear.min_level:
                new = max(level - fade_step(gear.fade_rate), gear.min_level)
        elif opcode in (STEP_UP, STEP_DOWN, STEP_DOWN_AND_OFF, ON_AND_STEP_UP):
            if level is None:
                update.query.add(gear.address)
            else:
                new = _step(gear, level, opcode)
        elif opcode == RECALL_MAX:
            new = gear.max_level
        elif opcode == RECALL_MIN:
            new = gear.min_level
        elif opcode == GO_TO_LAST_ACTIVE_LEVEL:
            if gear.last_active is None:
                update.query.add(gear.address)
            else:
                new = _clamp(gear, gear.last_active)
        elif GO_TO_SCENE <= opcode <= GO_TO_SCENE + 15:
            scene = gear.scenes.get(opcode - GO_TO_SCENE)
            if scene is None:
                update.query.add(gear.address)
            elif scene != MASK:
                new = _clamp(gear, scene) if scene else 0
        elif opcode in (CONTINUOUS_UP, CONTINUOUS_DOWN):
            update.query.add(gear.address)
        elif opcode == RESET:
            gear.min_level, gear.max_level = gear.physical_minimum, MAX_LEVEL
            update.query.add(gear.address)
        elif opcode == SET_MAX_LEVEL and self._dtr0 is not None:
            gear.max_level = min(max(self._dtr0, gear.min_level), MAX_LEVEL)
            if level and level > gear.max_level:
                new = gear.max_level
        elif opcode == SET_MIN_LEVEL and self._dtr0 is not None:
            gear.min_level = min(max(self._dtr0, gear.physical_minimum), gear.max_level)
            if level and level < gear.min_level:
                new = gear.min_level
        elif opcode == SET_FADE_RATE and self._dtr0 is not None:
            gear.fade_rate = self._dtr0 & 0x0F
        elif ADD_TO_GROUP <= opcode <= ADD_TO_GROUP + 15:
            gear.groups.add(opcode - ADD_TO_GROUP)
        elif REMOVE_FROM_GROUP <= opcode <= REMOVE_FROM_GROUP + 15:
            gear.groups.discard(opcode - REMOVE_FROM_GROUP)

        if new is not None and new != level:
            gear.set_level(new)
            update.changed.add(gear.address)


def _clamp(gear: Gear, value: int) -> int:
    """Arc power as the gear applies it: 0 is off, anything else within min..max."""
    if value == 0:
        return 0
    return min(max(value, gear.min_level), gear.max_level)


def _step(gear: Gear, level: int, opcode: int) -> int:
    """STEP commands, with the firmware's (IEC 62386-102) rules."""
    if opcode == STEP_UP:
        return level + 1 if gear.min_level <= level < gear.max_level else level
    if opcode == STEP_DOWN:
        return level - 1 if gear.min_level < level <= gear.max_level else level
    if opcode == STEP_DOWN_AND_OFF:
        if gear.min_level < level <= gear.max_level:
            return level - 1
        return 0 if level == gear.min_level else level
    # ON AND STEP UP
    if level == 0:
        return gear.min_level
    if gear.min_level <= level < gear.max_level:
        return level + 1
    return gear.max_level


def _highest(levels: list[int | None]) -> int | None:
    known = [lvl for lvl in levels if lvl is not None]
    return max(known) if known else None
