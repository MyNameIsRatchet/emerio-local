"""Diagnostic sensors for Emerio Local."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory

from .api import EmerioDevice
from .const import DOMAIN
from .entity import EmerioEntity
from .mapping import DP_FAULT


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities) -> None:
    device: EmerioDevice = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EmerioFaultSensor(device), EmerioStateSourceSensor(device)])


class EmerioFaultSensor(EmerioEntity, SensorEntity):
    _required_dps = frozenset({DP_FAULT})
    _attr_name = "Fehlercode"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, device: EmerioDevice) -> None:
        super().__init__(device, "fault")

    @property
    def native_value(self) -> int | None:
        return self.device.state.fault


class EmerioStateSourceSensor(EmerioEntity, SensorEntity):
    _attr_name = "Statusquelle"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["unknown", "optimistic", "device", "error"]
    _attr_icon = "mdi:lan-connect"

    def __init__(self, device: EmerioDevice) -> None:
        super().__init__(device, "state_source")

    @property
    def native_value(self) -> str:
        if self.device.last_error:
            return "error"
        return self.device.state.source
