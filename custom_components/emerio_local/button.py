"""Manual status refresh for Emerio Local."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory

from .api import EmerioDevice
from .const import DOMAIN
from .entity import EmerioEntity


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities) -> None:
    device: EmerioDevice = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EmerioRefreshButton(device)])


class EmerioRefreshButton(EmerioEntity, ButtonEntity):
    _attr_name = "Status aktualisieren"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:refresh"

    def __init__(self, device: EmerioDevice) -> None:
        super().__init__(device, "refresh")

    async def async_press(self) -> None:
        await self.device.async_refresh()
