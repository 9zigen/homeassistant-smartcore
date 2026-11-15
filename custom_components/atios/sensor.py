"""Sensor platform: DALI-2 light sensors (IEC 62386-304) and absolute inputs (-302).

Absolute input instances (sliders, rotary switches) become position sensors in
percent, read like the SmartCore firmware does (positions as 10-bit events, or
0..255 for models known to send them right aligned).

Light-sensor instances found in the SmartCore's NVRAM device model become
``sensor`` entities driven by the websocket bus monitor. The part-304 event
carries the illuminance as a 10-bit level in the event information field; we
expose that raw level as the state.

NOTE: converting the raw level to lux depends on the part-304 logarithmic
encoding and the instance's resolution setting, and is not implemented until
verified against real hardware (see CONTRIBUTING.md). The state is the raw
0..1023 level, clearly named as such.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AtiosConfigEntry
from .const import DOMAIN
from .dali import decode_input_event, position_percent
from .hub import MonitorFrame
from .nvram import INSTANCE_ABSOLUTE_INPUT, INSTANCE_LIGHT


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AtiosConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    entities: list[AtiosLightLevelSensor | AtiosPositionSensor] = []
    by_signature: dict[str, AtiosLightLevelSensor | AtiosPositionSensor] = {}
    for dev in hub.input_devices:
        for inst in dev.instances:
            sig = inst.expected_signature(dev.address)
            if sig is None or sig in by_signature:
                continue
            if inst.type == INSTANCE_LIGHT:
                entity = AtiosLightLevelSensor(
                    entry, signature=sig, label=f"{dev.name} light level {inst.number}"
                )
            elif inst.type == INSTANCE_ABSOLUTE_INPUT and inst.active:
                entity = AtiosPositionSensor(
                    entry,
                    signature=sig,
                    label=f"{dev.name} position {inst.number}",
                    right_aligned=dev.sends_right_aligned_positions(inst),
                )
            else:
                continue
            by_signature[sig] = entity
            entities.append(entity)
    if not entities:
        return
    async_add_entities(entities)

    @callback
    def on_monitor(event: MonitorFrame) -> None:
        if getattr(event, "framing_error", False):
            return
        decoded = decode_input_event(list(event.data), event.bits)
        if decoded is None or decoded.event_info is None:
            return
        entity = by_signature.get(decoded.signature)
        if entity is not None:
            entity.handle_event_info(decoded.event_info)

    entry.async_on_unload(hub.add_monitor_listener(on_monitor))


class AtiosLightLevelSensor(SensorEntity):
    """One DALI-2 light-sensor instance from the NVRAM model (raw level)."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = False

    def __init__(
        self, entry: AtiosConfigEntry, *, signature: str, label: str
    ) -> None:
        self._attr_name = label
        self._attr_unique_id = f"{entry.unique_id}_lux_{signature}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
        )
        self._attr_native_value = None

    @callback
    def handle_event_info(self, event_info: int) -> None:
        self._attr_native_value = event_info
        if self.hass is not None:
            self.async_write_ha_state()


class AtiosPositionSensor(SensorEntity):
    """One DALI-2 absolute input instance (IEC 62386-302): its position in percent."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = False

    def __init__(
        self, entry: AtiosConfigEntry, *, signature: str, label: str, right_aligned: bool
    ) -> None:
        self._right_aligned = right_aligned
        self._attr_name = label
        self._attr_unique_id = f"{entry.unique_id}_pos_{signature}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.unique_id)})
        self._attr_native_value = None  # unknown until the first event

    @callback
    def handle_event_info(self, event_info: int) -> None:
        self._attr_native_value = position_percent(event_info, self._right_aligned)
        self._attr_extra_state_attributes = {"event_info": event_info}
        if self.hass is not None:
            self.async_write_ha_state()
