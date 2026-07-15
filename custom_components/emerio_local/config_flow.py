"""UI configuration for Emerio Local."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant

from .api import (
    EmerioCommunicationError,
    InvalidLocalKey,
    probe_device_sync,
    validate_local_key,
)
from .const import CONF_DEVICE_ID, CONF_LOCAL_KEY, DEFAULT_NAME, DOMAIN

_LOGGER = logging.getLogger(__name__)


class EmerioLocalConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure the local device without requiring a cloud account."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await _async_validate_input(self.hass, user_input)
            except InvalidLocalKey:
                errors["base"] = "invalid_key"
            except EmerioCommunicationError:
                errors["base"] = "cannot_connect"
            except Exception:  # pragma: no cover - defensive HA config-flow boundary
                _LOGGER.exception("Unexpected error while probing the Emerio device")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(user_input[CONF_DEVICE_ID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_NAME], data=user_input
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_DEVICE_ID): str,
                vol.Required(CONF_LOCAL_KEY): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)


async def _async_validate_input(hass: HomeAssistant, data: dict) -> None:
    validate_local_key(data[CONF_LOCAL_KEY])
    try:
        await hass.async_add_executor_job(
            probe_device_sync,
            data[CONF_HOST],
            data[CONF_DEVICE_ID],
            data[CONF_LOCAL_KEY],
        )
    except InvalidLocalKey:
        raise
    except Exception as err:
        raise EmerioCommunicationError(str(err)) from err
