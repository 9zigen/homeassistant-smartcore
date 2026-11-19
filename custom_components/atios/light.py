"""Light platform for Atios SmartCore (raw DALI control gear).

Lights are created from the device model stored in the SmartCore's NVRAM (the
same list the DALI Configurator shows): every configured short address and
group becomes a named light, plus the bus-wide broadcast light. If the NVRAM
endpoint is unavailable (older firmware) the platform falls back to the legacy
per-address list from the options.

Brightness is sent as a DAPC level; plain on/off use GO TO LAST ACTIVE LEVEL /
OFF. Lights are not polled: every short address is queried once (QUERY ACTUAL
LEVEL) at start and after the DALI IP Interface socket reconnects, and from then
on the state follows the frames on the bus, ours and everyone else's
(gear_state.py, the same rules as the SmartCore firmware). Groups and "All
lights" show their brightest member.
"""

from __future__ import annotations

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_XY_COLOR,
    ColorMode,
    LightEntity,
)
import asyncio
import logging
from collections.abc import Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AtiosConfigEntry
from .const import CONF_LIGHTS, DOMAIN
from .dali import (
    Target,
    TargetType,
    dali_level_to_ha_brightness,
    dt8_activate,
    dt8_set_colour_temperature,
    dt8_set_xy,
    ha_brightness_to_dali,
    level,
    off,
    query_actual_level,
    goto_last_active,
)
from .gear_state import BusMirror, Gear, Update
from .hub import AtiosHub, MonitorFrame
from .nvram import ControlDevice

_LOGGER = logging.getLogger(__name__)

# A command whose result the frame does not tell (a scene without stored levels,
# CONTINUOUS UP / DOWN) is followed by a query of the gear, once a fade has had time to end
QUERY_AFTER_S = 1.5
# Lights are read once the DALI IP Interface socket is up (queries are cheap there);
# without a greeting within this time they are read over HTTP instead
START_QUERY_FALLBACK_S = 5

# Tc limits when a DT8 gear reports none (DALI default range 153..454 mireds)
DEFAULT_MIN_KELVIN = 2000
DEFAULT_MAX_KELVIN = 6500

# Control the lights one at a time; the hub serialises the DALI requests anyway
PARALLEL_UPDATES = 1


class LightStateTracker:
    """Keep the level of every gear current from the bus, query only when needed."""

    def __init__(self, hass: HomeAssistant, hub: AtiosHub, gears: list[Gear]) -> None:
        self._hass = hass
        self._hub = hub
        self.mirror = BusMirror(gears)
        self._listeners: list[Callable[[], None]] = []
        self._to_query: set[int] = set()
        self._query_task: asyncio.Task | None = None
        self._query_timer: asyncio.TimerHandle | None = None
        self._start_fallback: asyncio.TimerHandle | None = None

    def start(self) -> list[Callable[[], None]]:
        """Subscribe to the hub; returns the unsubscribe callbacks."""
        if self._hub.dali_ip_connected:
            self.query_all()
        else:
            self._start_fallback = self._hass.loop.call_later(
                START_QUERY_FALLBACK_S, self.query_all
            )
        return [
            self._hub.add_bus_listener(self._on_frame),
            self._hub.add_connected_listener(self.query_all),
            self._hub.add_bus_scan_listener(lambda active: None if active else self.query_all()),
            self._hub.add_ip_interface_listener(lambda on: self.query_all() if on else None),
            self._cancel,
        ]

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    @callback
    def query_all(self) -> None:
        """Read every gear again: at start, after a reconnect or a bus scan."""
        if self._start_fallback is not None:
            self._start_fallback.cancel()
            self._start_fallback = None
        self._query(set(self.mirror.gears), delay=0)

    @callback
    def _on_frame(self, frame: MonitorFrame) -> None:
        if frame.framing_error:
            return
        update = self.mirror.apply(list(frame.data), frame.bits)
        if update.query:
            self._query(update.query, delay=QUERY_AFTER_S)
        if update:
            self._notify(update)

    def _notify(self, _update: Update | None = None) -> None:
        for cb in list(self._listeners):
            cb()

    def _query(self, addresses: set[int], delay: float) -> None:
        self._to_query |= addresses
        if self._query_timer is not None or (self._query_task and not self._query_task.done()):
            return  # the running round picks the new addresses up
        if delay:
            self._query_timer = self._hass.loop.call_later(delay, self._start_round)
        else:
            self._start_round()

    @callback
    def _start_round(self) -> None:
        self._query_timer = None
        self._query_task = self._hass.async_create_background_task(
            self._run_queries(), "atios light state query"
        )

    async def _run_queries(self) -> None:
        while self._to_query:
            if self._hub.bus_scan_active:
                return  # read again once the scan has ended
            address = min(self._to_query)
            self._to_query.discard(address)
            answer = await self._hub.send_frame(query_actual_level(Target.short(address)))
            if answer and self.mirror.set_level(address, answer[0]):
                self._notify()

    def _cancel(self) -> None:
        if self._start_fallback is not None:
            self._start_fallback.cancel()
            self._start_fallback = None
        if self._query_timer is not None:
            self._query_timer.cancel()
            self._query_timer = None
        if self._query_task is not None:
            self._query_task.cancel()


def _gears(hub: AtiosHub, legacy: list[int]) -> list[Gear]:
    if not hub.control_devices:
        return [Gear(address) for address in legacy]
    return [
        Gear(
            dev.number,
            groups=set(dev.gear_groups),
            min_level=dev.min_level,
            max_level=dev.max_level,
            physical_minimum=dev.physical_minimum,
            fade_rate=dev.fade_rate,
            scenes=dict(dev.scenes),
        )
        for dev in hub.control_devices
        if not dev.is_group
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AtiosConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = entry.runtime_data

    legacy: list[int] = []
    if not hub.control_devices:
        # legacy fallback: per-address list from the options
        for addr in entry.options.get(CONF_LIGHTS, []):
            try:
                legacy.append(Target.short(int(addr)).number)
            except (ValueError, TypeError):
                continue
    tracker = LightStateTracker(hass, hub, _gears(hub, legacy))

    entities: list[AtiosLight] = [AtiosLight(hub, entry, tracker, Target.broadcast())]
    if hub.control_devices:
        # named lights from the NVRAM device model (groups + short addresses)
        for dev in hub.control_devices:
            entities.append(
                AtiosLight(hub, entry, tracker, dev.target, name=dev.name, device=dev)
            )
    else:
        entities += [AtiosLight(hub, entry, tracker, Target.short(a)) for a in legacy]
    async_add_entities(entities)
    for unsubscribe in tracker.start():
        entry.async_on_unload(unsubscribe)


class AtiosLight(LightEntity):
    """A DALI control-gear target exposed as an HA light."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(
        self,
        hub: AtiosHub,
        entry: AtiosConfigEntry,
        tracker: LightStateTracker,
        target: Target,
        name: str | None = None,
        device: ControlDevice | None = None,
    ) -> None:
        self._hub = hub
        self._tracker = tracker
        self._target = target
        self._device = device

        # DT8 colour / tunable white, from the NVRAM colour capabilities
        modes = {ColorMode.BRIGHTNESS}
        if device is not None and device.supports_dt8:
            if device.tc_capable:
                modes.add(ColorMode.COLOR_TEMP)
                warm = device.ct_warmest_mired
                cool = device.ct_coolest_mired
                self._attr_min_color_temp_kelvin = (
                    round(1_000_000 / warm) if warm else DEFAULT_MIN_KELVIN
                )
                self._attr_max_color_temp_kelvin = (
                    round(1_000_000 / cool) if cool else DEFAULT_MAX_KELVIN
                )
            if device.xy_capable:
                modes.add(ColorMode.XY)
        if len(modes) > 1:
            modes.discard(ColorMode.BRIGHTNESS)  # HA: BRIGHTNESS never mixes
        self._attr_supported_color_modes = modes
        self._attr_color_mode = (
            ColorMode.BRIGHTNESS if modes == {ColorMode.BRIGHTNESS} else next(iter(modes))
        )
        self._attr_unique_id = f"{entry.unique_id}_{target.unique_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
            manufacturer="Atios",
            model="SmartCore",
            name=f"SmartCore ({hub.host})",
            serial_number=hub.serial,
            sw_version=hub.sw_version,
        )
        if name:
            self._attr_name = name
        elif target.type is TargetType.BROADCAST:
            self._attr_name = "All lights"
        elif target.type is TargetType.GROUP:
            self._attr_name = f"Group {target.number}"
        else:
            self._attr_name = f"Light {target.number}"
        self._attr_is_on: bool | None = None
        self._attr_brightness: int | None = None
        self._read_state()

    @property
    def available(self) -> bool:
        """Unavailable while the SmartCore does not answer DALI requests at all."""
        return self._hub.available

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._hub.add_available_listener(self._on_available_changed))
        self.async_on_remove(self._tracker.add_listener(self._on_state_changed))
        self._read_state()  # levels read before the entity was added

    @callback
    def _on_available_changed(self, _available: bool) -> None:
        self.async_write_ha_state()

    def _level(self) -> int | None:
        mirror = self._tracker.mirror
        if self._target.type is TargetType.SHORT:
            return mirror.level(self._target.number)
        if self._target.type is TargetType.GROUP:
            return mirror.group_level(self._target.number)
        return mirror.all_level()

    def _read_state(self) -> bool:
        """Take the level from the bus mirror; True when the state changed."""
        mirror = self._tracker.mirror
        if self._target.type is TargetType.GROUP:
            # a group without known members only has what was sent to it
            self._attr_assumed_state = not mirror.members(self._target.number)
        elif self._target.type is TargetType.BROADCAST:
            self._attr_assumed_state = not mirror.gears
        lvl = self._level()
        is_on = None if lvl is None else lvl > 0
        brightness = dali_level_to_ha_brightness(lvl) if lvl else None
        changed = (is_on, brightness) != (self._attr_is_on, self._attr_brightness)
        self._attr_is_on, self._attr_brightness = is_on, brightness
        return changed

    @callback
    def _on_state_changed(self) -> None:
        if self._read_state():
            self.async_write_ha_state()

    def _raise_if_bus_scan(self) -> None:
        if self._hub.bus_scan_active:
            raise HomeAssistantError(
                f"A DALI bus scan is running on the SmartCore ({self._hub.host}); "
                "lights can be controlled again once it has finished."
            )

    async def async_turn_on(self, **kwargs) -> None:
        self._raise_if_bus_scan()

        # Set the temporary DT8 colour first; it becomes active with the DAPC
        # level below, or with an explicit ACTIVATE when no brightness is given.
        colour_set = False
        modes = self._attr_supported_color_modes or set()
        if ATTR_COLOR_TEMP_KELVIN in kwargs and ColorMode.COLOR_TEMP in modes:
            kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            mireds = round(1_000_000 / max(1, kelvin))
            for frame in dt8_set_colour_temperature(self._target, mireds):
                await self._hub.send_frame(frame)
            self._attr_color_temp_kelvin = kelvin
            self._attr_color_mode = ColorMode.COLOR_TEMP
            colour_set = True
        elif ATTR_XY_COLOR in kwargs and ColorMode.XY in modes:
            x, y = kwargs[ATTR_XY_COLOR]
            for frame in dt8_set_xy(self._target, x, y):
                await self._hub.send_frame(frame)
            self._attr_xy_color = (x, y)
            self._attr_color_mode = ColorMode.XY
            colour_set = True

        if ATTR_BRIGHTNESS in kwargs:
            dali = ha_brightness_to_dali(kwargs[ATTR_BRIGHTNESS])
            await self._hub.send_frame(level(self._target, dali))
        else:
            # Plain "on": restore the level the gear had before it was switched
            # off (GO TO LAST ACTIVE LEVEL), rather than forcing full brightness.
            await self._hub.send_frame(goto_last_active(self._target))
            if colour_set:
                # no DAPC was sent, so apply the temporary colour explicitly
                for frame in dt8_activate(self._target):
                    await self._hub.send_frame(frame)
        # The new level arrives through the bus mirror; colour is kept as sent
        if colour_set:
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._raise_if_bus_scan()
        await self._hub.send_frame(off(self._target))
