"""Climate entity for the Emerio air conditioner."""

from __future__ import annotations

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    HVACAction,
    HVACMode,
    PRESET_NONE,
    ClimateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_WHOLE, UnitOfTemperature

from .api import EmerioDevice
from .const import DOMAIN
from .entity import EmerioEntity
from .mapping import (
    DP_FAN_MODE,
    DP_HVAC_MODE,
    DP_POWER,
    DP_SLEEP,
    DP_TARGET_TEMPERATURE,
    FAN_TO_TUYA,
    hvac_write_sequence,
)


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities) -> None:
    device: EmerioDevice = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EmerioClimate(device)])


class EmerioClimate(EmerioEntity, ClimateEntity):
    """Climate control backed by real updates with an optimistic fallback."""

    _attr_name = None
    _required_dps = frozenset(
        {
            DP_POWER,
            DP_TARGET_TEMPERATURE,
            DP_HVAC_MODE,
            DP_FAN_MODE,
            DP_SLEEP,
        }
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = PRECISION_WHOLE
    _attr_target_temperature_step = 1
    _attr_min_temp = 16
    _attr_max_temp = 31
    _attr_hvac_modes = [
        HVACMode.OFF,
        HVACMode.COOL,
        HVACMode.DRY,
        HVACMode.FAN_ONLY,
    ]
    _attr_fan_modes = ["high", "low"]
    _attr_preset_modes = [PRESET_NONE, "sleep"]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, device: EmerioDevice) -> None:
        super().__init__(device, "climate")

    @property
    def hvac_mode(self) -> HVACMode:
        if not self.device.state.power:
            return HVACMode.OFF
        return HVACMode(self.device.state.hvac_mode)

    @property
    def hvac_action(self) -> HVACAction:
        if not self.device.state.power:
            return HVACAction.OFF
        return {
            "cool": HVACAction.COOLING,
            "dry": HVACAction.DRYING,
            "fan_only": HVACAction.FAN,
        }[self.device.state.hvac_mode]

    @property
    def target_temperature(self) -> float:
        return self.device.state.target_temperature

    @property
    def current_temperature(self) -> float | None:
        return self.device.state.current_temperature

    @property
    def fan_mode(self) -> str:
        return self.device.state.fan_mode

    @property
    def preset_mode(self) -> str:
        return "sleep" if self.device.state.sleep else PRESET_NONE

    async def async_turn_on(self) -> None:
        await self.device.async_write_dps({DP_POWER: True})

    async def async_turn_off(self) -> None:
        await self.device.async_write_dps({DP_POWER: False})

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
            return
        # This Emerio firmware accepts the individual DPs but ignores the mode
        # when power and mode are combined in one Tuya command.
        writes = hvac_write_sequence(self.device.state.power, hvac_mode.value)
        for index, dps in enumerate(writes):
            await self.device.async_write_dps(dps)
            if index < len(writes) - 1:
                # The mode command is ignored until the firmware has confirmed
                # that its transition from off to on is complete.
                await self.device.async_wait_for_device_dp(DP_POWER, True)

    async def async_set_temperature(self, **kwargs) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        temperature = max(self.min_temp, min(self.max_temp, round(temperature)))
        await self.device.async_write_dps({DP_TARGET_TEMPERATURE: temperature})

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self.device.async_write_dps({DP_FAN_MODE: FAN_TO_TUYA[fan_mode]})

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        await self.device.async_write_dps({DP_SLEEP: preset_mode == "sleep"})
