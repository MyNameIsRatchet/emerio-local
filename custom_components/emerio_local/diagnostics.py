"""Diagnostics support for Emerio Local."""

from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_LOCAL_KEY, DOMAIN

TO_REDACT = {CONF_LOCAL_KEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    device = hass.data[DOMAIN][entry.entry_id]
    return {
        "config": async_redact_data(dict(entry.data), TO_REDACT),
        "runtime": {
            "state": {
                "power": device.state.power,
                "target_temperature": device.state.target_temperature,
                "current_temperature": device.state.current_temperature,
                "hvac_mode": device.state.hvac_mode,
                "fan_mode": device.state.fan_mode,
                "sleep": device.state.sleep,
                "timer": device.state.timer,
                "fault": device.state.fault,
                "source": device.state.source,
                "confirmed_dps": sorted(device.state.confirmed_dps),
            },
            "command_reachable": device.command_reachable,
            "monitor_connected": device.monitor_connected,
            "last_command": device.last_command,
            "last_command_at": _isoformat(device.last_command_at),
            "last_status_at": _isoformat(device.last_status_at),
            "last_connect_at": _isoformat(device.last_connect_at),
            "last_disconnect_at": _isoformat(device.last_disconnect_at),
            "last_device_dps": device.last_device_dps,
            "last_error": device.last_error,
        },
    }


def _isoformat(value):
    return value.isoformat() if value is not None else None
