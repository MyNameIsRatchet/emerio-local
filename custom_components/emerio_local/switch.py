"""Power and sleep switches for Emerio Local."""

from __future__ import annotations

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry

from .api import EmerioDevice
from .const import DOMAIN
from .entity import EmerioEntity
from .mapping import DP_POWER, DP_SLEEP


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities) -> None:
    device: EmerioDevice = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EmerioPowerSwitch(device), EmerioSleepSwitch(device)])


class EmerioPowerSwitch(EmerioEntity, SwitchEntity):
    _required_dps = frozenset({DP_POWER})
    _attr_name = "Power"
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:power"

    def __init__(self, device: EmerioDevice) -> None:
        super().__init__(device, "power")

    @property
    def is_on(self) -> bool:
        return self.device.state.power

    async def async_turn_on(self, **kwargs) -> None:
        await self.device.async_write_dps({DP_POWER: True})

    async def async_turn_off(self, **kwargs) -> None:
        await self.device.async_write_dps({DP_POWER: False})


class EmerioSleepSwitch(EmerioEntity, SwitchEntity):
    _required_dps = frozenset({DP_SLEEP})
    _attr_name = "Schlafmodus"
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:power-sleep"

    def __init__(self, device: EmerioDevice) -> None:
        super().__init__(device, "sleep")

    @property
    def is_on(self) -> bool:
        return self.device.state.sleep

    async def async_turn_on(self, **kwargs) -> None:
        await self.device.async_write_dps({DP_SLEEP: True})

    async def async_turn_off(self, **kwargs) -> None:
        await self.device.async_write_dps({DP_SLEEP: False})
