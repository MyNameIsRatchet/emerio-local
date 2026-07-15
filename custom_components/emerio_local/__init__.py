"""Emerio Local integration setup."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant

from .api import EmerioDevice
from .const import CONF_DEVICE_ID, CONF_LOCAL_KEY, DOMAIN, PLATFORMS


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one Emerio air conditioner."""

    device = EmerioDevice(
        hass=hass,
        name=entry.data[CONF_NAME],
        host=entry.data[CONF_HOST],
        device_id=entry.data[CONF_DEVICE_ID],
        local_key=entry.data[CONF_LOCAL_KEY],
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = device
    await device.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an Emerio air conditioner."""

    device: EmerioDevice = hass.data[DOMAIN][entry.entry_id]
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await device.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
