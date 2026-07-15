"""Timer control for Emerio Local."""

from __future__ import annotations

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime

from .api import EmerioDevice
from .const import DOMAIN
from .entity import EmerioEntity
from .mapping import DP_TIMER


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities) -> None:
    device: EmerioDevice = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EmerioTimer(device)])


class EmerioTimer(EmerioEntity, NumberEntity):
    _required_dps = frozenset({DP_TIMER})
    _attr_name = "Timer"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_native_min_value = 0
    _attr_native_max_value = 24
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:timer-outline"

    def __init__(self, device: EmerioDevice) -> None:
        super().__init__(device, "timer")

    @property
    def native_value(self) -> int:
        return self.device.state.timer

    async def async_set_native_value(self, value: float) -> None:
        await self.device.async_write_dps({DP_TIMER: int(value)})
