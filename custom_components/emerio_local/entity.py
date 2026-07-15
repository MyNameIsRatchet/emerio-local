"""Shared entity base for Emerio Local."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .api import EmerioDevice
from .const import DOMAIN


class EmerioEntity(Entity):
    """An entity backed by the shared device state."""

    _attr_has_entity_name = True
    _required_dps: frozenset[int] = frozenset()

    def __init__(self, device: EmerioDevice, suffix: str) -> None:
        self.device = device
        self._attr_unique_id = f"{device.device_id}_{suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            name=device.name,
            manufacturer="Emerio",
            model="PAC-127111.1",
            sw_version="Tuya protocol 3.4",
        )

    @property
    def available(self) -> bool:
        """Stay controllable so a failed command can always be retried."""

        return True

    @property
    def assumed_state(self) -> bool:
        """Only mark the state assumed until it was confirmed by the device."""

        if self._required_dps:
            return not self._required_dps.issubset(self.device.state.confirmed_dps)
        return self.device.state.source != "device"

    @property
    def extra_state_attributes(self):
        return {
            "state_source": self.device.state.source,
            "command_reachable": self.device.command_reachable,
            "monitor_connected": self.device.monitor_connected,
            "last_command": self.device.last_command,
            "last_command_at": self.device.last_command_at,
            "last_status_at": self.device.last_status_at,
            "last_error": self.device.last_error,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.device.add_listener(self.async_write_ha_state))
