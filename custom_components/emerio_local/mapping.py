"""Emerio PAC-127111.1 datapoint mapping and state handling."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DP_POWER = 1
DP_TARGET_TEMPERATURE = 2
DP_CURRENT_TEMPERATURE = 3
DP_FAULT = 20
DP_HVAC_MODE = 101
DP_SLEEP = 103
DP_FAN_MODE = 104
DP_TIMER = 105
DP_TEMPERATURE_UNIT = 109
DP_TARGET_TEMPERATURE_F = 110
DP_CURRENT_TEMPERATURE_F = 111

KNOWN_DPS = (
    DP_POWER,
    DP_TARGET_TEMPERATURE,
    DP_CURRENT_TEMPERATURE,
    DP_FAULT,
    DP_HVAC_MODE,
    DP_SLEEP,
    DP_FAN_MODE,
    DP_TIMER,
    DP_TEMPERATURE_UNIT,
    DP_TARGET_TEMPERATURE_F,
    DP_CURRENT_TEMPERATURE_F,
)

HVAC_TO_TUYA = {
    "cool": "1",
    "dry": "3",
    "fan_only": "5",
}
TUYA_TO_HVAC = {value: key for key, value in HVAC_TO_TUYA.items()}

FAN_TO_TUYA = {
    "high": "1",
    "low": "3",
}
TUYA_TO_FAN = {value: key for key, value in FAN_TO_TUYA.items()}


@dataclass(slots=True)
class EmerioState:
    """The best state currently known to Home Assistant."""

    power: bool = False
    target_temperature: int = 24
    current_temperature: float | None = None
    hvac_mode: str = "cool"
    fan_mode: str = "low"
    sleep: bool = False
    timer: int = 0
    fault: int | None = None
    temperature_unit: str = "C"
    source: str = "unknown"
    confirmed_dps: set[int] = field(default_factory=set)


def apply_dps(state: EmerioState, dps: dict[int | str, Any], source: str) -> bool:
    """Apply Tuya datapoints and return whether at least one value was recognised."""

    recognised = False
    for raw_dp, value in dps.items():
        try:
            dp = int(raw_dp)
        except (TypeError, ValueError):
            continue

        if dp == DP_POWER and isinstance(value, bool):
            state.power = value
        elif dp == DP_TARGET_TEMPERATURE and _is_number(value):
            state.target_temperature = int(value)
        elif dp == DP_CURRENT_TEMPERATURE and _is_number(value):
            state.current_temperature = float(value)
        elif dp == DP_FAULT and _is_number(value):
            state.fault = int(value)
        elif dp == DP_HVAC_MODE and str(value) in TUYA_TO_HVAC:
            state.hvac_mode = TUYA_TO_HVAC[str(value)]
        elif dp == DP_SLEEP and isinstance(value, bool):
            state.sleep = value
        elif dp == DP_FAN_MODE and str(value) in TUYA_TO_FAN:
            state.fan_mode = TUYA_TO_FAN[str(value)]
        elif dp == DP_TIMER and _is_number(value):
            state.timer = int(value)
        elif dp == DP_TEMPERATURE_UNIT and isinstance(value, bool):
            state.temperature_unit = "F" if value else "C"
        elif dp == DP_TARGET_TEMPERATURE_F and _is_number(value):
            if state.temperature_unit == "F":
                state.target_temperature = round((float(value) - 32) * 5 / 9)
        elif dp == DP_CURRENT_TEMPERATURE_F and _is_number(value):
            if state.temperature_unit == "F":
                state.current_temperature = (float(value) - 32) * 5 / 9
        else:
            continue

        recognised = True
        if source == "device":
            state.confirmed_dps.add(dp)
        elif source == "optimistic":
            state.confirmed_dps.discard(dp)

    if recognised:
        state.source = source
    return recognised


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
