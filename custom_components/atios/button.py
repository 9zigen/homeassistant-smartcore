"""Button platform: reload the DALI device model from the SmartCore.

After devices were added, removed or regrouped in the SmartCore's DALI
configurator, "Reload DALI devices" reloads the integration: the NVRAM device
model is read again, entities are created for new devices and every light is
queried once.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AtiosConfigEntry
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AtiosConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([AtiosReloadDaliButton(entry)])


class AtiosReloadDaliButton(ButtonEntity):
    """Read the DALI devices from the SmartCore again."""

    _attr_has_entity_name = True
    _attr_name = "Reload DALI devices"
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: AtiosConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.unique_id}_reload_dali"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.unique_id)})

    async def async_press(self) -> None:
        self.hass.config_entries.async_schedule_reload(self._entry.entry_id)
