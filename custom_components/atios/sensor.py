"""Sensor platform: DALI-2 light sensors (IEC 62386-304), absolute inputs (-302)
and general purpose sensors (-306).

General purpose instances (temperature, humidity, CO2, ...) report their
measurement, scaled and with the unit from the SmartCore's NVRAM model.

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

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AtiosConfigEntry
from .const import DOMAIN
from .dali import decode_input_event, position_percent
from .general_purpose import Measurement
from .hub import MonitorFrame
from .nvram import INSTANCE_ABSOLUTE_INPUT, INSTANCE_GENERAL_PURPOSE, INSTANCE_LIGHT


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AtiosConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    async_add_entities(
        [
            AtiosDeviceCountSensor(entry, "control"),
            AtiosDeviceCountSensor(entry, "input"),
        ]
    )
    entities: list[_InstanceSensor] = []
    by_signature: dict[str, _InstanceSensor] = {}
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
            elif inst.type == INSTANCE_GENERAL_PURPOSE and inst.resolution > 0:
                measurement = Measurement(
                    resolution=inst.resolution,
                    magnitude=inst.magnitude,
                    signed=inst.input_signal_signed,
                    quantity_name=inst.quantity_name,
                    unit_of_measure=inst.unit_of_measure,
                )
                entity = AtiosGeneralPurposeSensor(
                    entry,
                    signature=sig,
                    label=f"{dev.name} {measurement.label} {inst.number}",
                    measurement=measurement,
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


class AtiosGeneralPurposeSensor(SensorEntity):
    """One DALI-2 general purpose sensor instance (IEC 62386-306)."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = False

    def __init__(
        self, entry: AtiosConfigEntry, *, signature: str, label: str, measurement: Measurement
    ) -> None:
        self._measurement = measurement
        self._attr_name = label
        self._attr_unique_id = f"{entry.unique_id}_gp_{signature}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.unique_id)})
        self._attr_native_unit_of_measurement = measurement.unit
        if measurement.device_class is not None:
            self._attr_device_class = SensorDeviceClass(measurement.device_class)
        self._attr_suggested_display_precision = measurement.decimals
        self._attr_native_value = None  # unknown until the first report

    @callback
    def handle_event_info(self, event_info: int) -> None:
        value = self._measurement.decode_event(event_info)
        if value is None:  # alarm events and MASK keep the last measurement
            return
        self._attr_native_value = value
        self._attr_extra_state_attributes = {"event_info": event_info}
        if self.hass is not None:
            self.async_write_ha_state()



class AtiosDeviceCountSensor(SensorEntity):
    """How many DALI control or input devices were loaded from the SmartCore's NVRAM."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = False

    def __init__(self, entry: AtiosConfigEntry, kind: str) -> None:
        hub = entry.runtime_data
        self._attr_unique_id = f"{entry.unique_id}_{kind}_devices"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.unique_id)})
        if kind == "control":
            self._attr_name = "DALI control devices"
            self._attr_icon = "mdi:lightbulb-group"
            gear = [d for d in hub.control_devices if not d.is_group]
            groups = [d for d in hub.control_devices if d.is_group]
            self._attr_native_value = len(gear) if hub.nvm_loaded else None
            self._attr_extra_state_attributes = {
                "groups": len(groups),
                "devices": [f"A{d.number} {d.name}" for d in gear],
            }
        else:
            self._attr_name = "DALI input devices"
            self._attr_icon = "mdi:gesture-tap-button"
            self._attr_native_value = len(hub.input_devices) if hub.nvm_loaded else None
            self._attr_extra_state_attributes = {
                "instances": sum(len(d.instances) for d in hub.input_devices),
                "devices": [f"A{d.address} {d.name}" for d in hub.input_devices],
            }


_InstanceSensor = AtiosLightLevelSensor | AtiosPositionSensor | AtiosGeneralPurposeSensor
