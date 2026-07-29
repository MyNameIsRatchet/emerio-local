"""Emerio Local integration setup."""

from __future__ import annotations

import math

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_FRIENDLY_NAME,
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_HOST,
    CONF_NAME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfPower,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.unit_conversion import PowerConverter

from .api import EmerioDevice
from .const import (
    CONF_COMPRESSOR_THRESHOLD,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_POWER_ON_THRESHOLD,
    CONF_POWER_SENSOR,
    DEFAULT_COMPRESSOR_THRESHOLD,
    DEFAULT_POWER_ON_THRESHOLD,
    DOMAIN,
    PLATFORMS,
)


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
    power_sensor = entry.options.get(CONF_POWER_SENSOR) or _find_power_sensor(
        hass, device.name
    )
    if power_sensor:
        device.configure_power_fallback(
            power_sensor,
            float(
                entry.options.get(
                    CONF_POWER_ON_THRESHOLD, DEFAULT_POWER_ON_THRESHOLD
                )
            ),
            float(
                entry.options.get(
                    CONF_COMPRESSOR_THRESHOLD, DEFAULT_COMPRESSOR_THRESHOLD
                )
            ),
        )

        @callback
        def _handle_power_change(
            event: Event[EventStateChangedData],
        ) -> None:
            _apply_power_state(device, event.data["new_state"])

        entry.async_on_unload(
            async_track_state_change_event(
                hass,
                [power_sensor],
                _handle_power_change,
            )
        )
        _apply_power_state(device, hass.states.get(power_sensor))

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


def _find_power_sensor(hass: HomeAssistant, device_name: str) -> str | None:
    """Find one clearly named power sensor when no explicit option is set."""

    tokens = {
        token
        for token in device_name.casefold().replace("-", " ").split()
        if len(token) >= 5 and token not in {"emerio", "local"}
    }
    candidates: list[str] = []
    for state in hass.states.async_all("sensor"):
        if state.attributes.get(ATTR_DEVICE_CLASS) != SensorDeviceClass.POWER:
            continue
        searchable = " ".join(
            (
                state.entity_id.replace("_", " "),
                str(state.attributes.get(ATTR_FRIENDLY_NAME, "")),
            )
        ).casefold()
        if tokens and any(token in searchable for token in tokens):
            candidates.append(state.entity_id)
    return candidates[0] if len(candidates) == 1 else None


@callback
def _apply_power_state(device: EmerioDevice, state: State | None) -> None:
    """Convert a Home Assistant power sensor state to watts."""

    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return
    try:
        watts = float(state.state)
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if unit and unit != UnitOfPower.WATT:
            watts = PowerConverter.convert(watts, unit, UnitOfPower.WATT)
    except (TypeError, ValueError):
        return
    if not math.isfinite(watts):
        return
    device.apply_power_fallback(max(0.0, watts))
