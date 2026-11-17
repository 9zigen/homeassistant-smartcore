"""Light platform for Atios SmartCore (raw DALI control gear).

Lights are created from the device model stored in the SmartCore's NVRAM (the
same list the DALI Configurator shows): every configured short address and
group becomes a named light, plus the bus-wide broadcast light. If the NVRAM
endpoint is unavailable (older firmware) the platform falls back to the legacy
per-address list from the options.

Brightness is sent as a DAPC level; plain on/off use GO TO LAST ACTIVE LEVEL /
OFF. State is read
back with QUERY ACTUAL LEVEL where the target is a single address (broadcast
and group queries collide on the bus, so those stay optimistic).
"""

from __future__ import annotations

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_XY_COLOR,
    ColorMode,
    LightEntity,
)
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
from .hub import AtiosHub
from .nvram import ControlDevice

# Tc limits when a DT8 gear reports none (DALI default range 153..454 mireds)
DEFAULT_MIN_KELVIN = 2000
DEFAULT_MAX_KELVIN = 6500

# Update and control the lights one at a time. Every poll is a QUERY ACTUAL
# LEVEL that the SmartCore answers in its single web server task; the hub
# serialises the DALI requests anyway, this keeps HA from starting them all.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AtiosConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = entry.runtime_data

    entities: list[AtiosLight] = [AtiosLight(hub, entry, Target.broadcast())]
    if hub.control_devices:
        # named lights from the NVRAM device model (groups + short addresses)
        for dev in hub.control_devices:
            entities.append(AtiosLight(hub, entry, dev.target, name=dev.name, device=dev))
    else:
        # legacy fallback: per-address list from the options
        for addr in entry.options.get(CONF_LIGHTS, []):
            try:
                entities.append(AtiosLight(hub, entry, Target.short(int(addr))))
            except (ValueError, TypeError):
                continue
    async_add_entities(entities)


class AtiosLight(LightEntity):
    """A DALI control-gear target exposed as an HA light."""

    _attr_has_entity_name = True
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(
        self,
        hub: AtiosHub,
        entry: AtiosConfigEntry,
        target: Target,
        name: str | None = None,
        device: ControlDevice | None = None,
    ) -> None:
        self._hub = hub
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
        # broadcast/group can't be reliably queried -> optimistic
        self._attr_assumed_state = target.type is not TargetType.SHORT
        self._attr_is_on = False
        self._attr_brightness: int | None = None

    @property
    def available(self) -> bool:
        """Unavailable while the SmartCore does not answer DALI requests at all."""
        return self._hub.available

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._hub.add_available_listener(self._on_available_changed))

    @callback
    def _on_available_changed(self, _available: bool) -> None:
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
            self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
        else:
            # Plain "on": restore the level the gear had before it was switched
            # off (GO TO LAST ACTIVE LEVEL), rather than forcing full brightness.
            await self._hub.send_frame(goto_last_active(self._target))
            if colour_set:
                # no DAPC was sent, so apply the temporary colour explicitly
                for frame in dt8_activate(self._target):
                    await self._hub.send_frame(frame)
            # Actual level is read back by async_update for single addresses;
            # optimistic only for broadcast/group.
            if self._target.type is not TargetType.SHORT and self._attr_brightness is None:
                self._attr_brightness = 255
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._raise_if_bus_scan()
        await self._hub.send_frame(off(self._target))
        self._attr_is_on = False
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Poll actual level for single-address lights only, not during a bus scan."""
        if self._target.type is not TargetType.SHORT or self._hub.bus_scan_active:
            return
        answer = await self._hub.send_frame(query_actual_level(self._target))
        if not answer:
            return
        lvl = answer[0]
        if lvl in (255, None):  # MASK / no answer
            return
        self._attr_is_on = lvl > 0
        self._attr_brightness = dali_level_to_ha_brightness(lvl) if lvl > 0 else None
