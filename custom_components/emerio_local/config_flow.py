"""UI configuration for Emerio Local."""

from __future__ import annotations

import logging
from typing import Any

import tinytuya
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    QrCodeSelector,
    QrCodeSelectorConfig,
    QrErrorCorrectionLevel,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import (
    EmerioCommunicationError,
    InvalidLocalKey,
    probe_device_sync,
    validate_local_key,
)
from .cloud import TuyaCloudDevice, TuyaCloudError, TuyaCloudSession
from .const import (
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_SETUP_METHOD,
    CONF_USER_CODE,
    DEFAULT_NAME,
    DOMAIN,
    SETUP_METHOD_CLOUD,
    SETUP_METHOD_MANUAL,
    SUPPORTED_PRODUCT_IDS,
)

_LOGGER = logging.getLogger(__name__)


class EmerioLocalConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure an Emerio device manually or with Smart Life assistance."""

    VERSION = 1

    def __init__(self) -> None:
        self._cloud: TuyaCloudSession | None = None
        self._cloud_devices: dict[str, TuyaCloudDevice] = {}
        self._cloud_device: TuyaCloudDevice | None = None
        self._discovered_host = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose cloud-assisted or manual setup."""

        if user_input is not None:
            if user_input[CONF_SETUP_METHOD] == SETUP_METHOD_CLOUD:
                return await self.async_step_cloud()
            return await self.async_step_manual()

        setup_selector = SelectSelector(
            SelectSelectorConfig(
                options=[SETUP_METHOD_CLOUD, SETUP_METHOD_MANUAL],
                mode=SelectSelectorMode.LIST,
                translation_key=CONF_SETUP_METHOD,
            )
        )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_SETUP_METHOD): setup_selector}),
        )

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Request a Tuya QR token using the Smart Life user code."""

        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        self._cloud = self._cloud or TuyaCloudSession()

        if user_input is not None:
            try:
                await self._cloud.async_request_qr_code(
                    self.hass, user_input[CONF_USER_CODE]
                )
            except TuyaCloudError as err:
                errors["base"] = "cloud_login"
                placeholders = err.placeholders
            except Exception:  # pragma: no cover - defensive cloud boundary
                _LOGGER.exception("Unexpected error requesting a Tuya QR code")
                errors["base"] = "unknown"
            else:
                return await self.async_step_scan()

        defaults = user_input or {}
        return self.async_show_form(
            step_id="cloud",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USER_CODE,
                        default=defaults.get(CONF_USER_CODE, ""),
                    ): str
                }
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_scan(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Display the QR code and finish Tuya authorization."""

        if self._cloud is None or self._cloud.qr_code is None:
            return await self.async_step_cloud()

        if user_input is None:
            return self._show_scan_form()

        try:
            devices = await self._cloud.async_login_and_get_devices(self.hass)
        except TuyaCloudError as err:
            placeholders = err.placeholders
            try:
                if self._cloud.user_code is not None:
                    await self._cloud.async_request_qr_code(
                        self.hass, self._cloud.user_code
                    )
            except TuyaCloudError:
                pass
            return self._show_scan_form(
                errors={"base": "cloud_login"},
                placeholders=placeholders,
            )
        except Exception:  # pragma: no cover - defensive cloud boundary
            _LOGGER.exception("Unexpected error completing Tuya QR login")
            return self._show_scan_form(errors={"base": "unknown"})

        self._cloud = None
        self._cloud_devices = {
            device.device_id: device
            for device in devices
            if device.product_id in SUPPORTED_PRODUCT_IDS
        }
        if not self._cloud_devices:
            return self.async_abort(reason="no_supported_devices")
        return await self.async_step_choose_device()

    async def async_step_choose_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose one compatible device returned by Smart Life."""

        if user_input is not None:
            device = self._cloud_devices.get(user_input[CONF_DEVICE_ID])
            if device is None:
                return self.async_abort(reason="no_supported_devices")
            self._cloud_device = device
            return await self.async_step_discover()

        options = []
        for device in self._cloud_devices.values():
            detail = device.product_name or device.product_id
            offline = " — offline" if not device.online else ""
            options.append(
                SelectOptionDict(
                    value=device.device_id,
                    label=f"{device.name} ({detail}){offline}",
                )
            )
        device_selector = SelectSelector(
            SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
        )
        return self.async_show_form(
            step_id="choose_device",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE_ID): device_selector}),
        )

    async def async_step_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Find the selected device on the local network."""

        if self._cloud_device is None:
            return self.async_abort(reason="no_supported_devices")

        if user_input is not None:
            try:
                self._discovered_host = await self.hass.async_add_executor_job(
                    _discover_device_sync, self._cloud_device.device_id
                )
            except Exception:
                _LOGGER.warning(
                    "Local discovery failed for the selected Tuya device",
                    exc_info=True,
                )
                self._discovered_host = ""
            return await self.async_step_cloud_device()

        return self.async_show_form(
            step_id="discover",
            data_schema=vol.Schema({}),
        )

    async def async_step_cloud_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm local details without exposing the retrieved Local Key."""

        if self._cloud_device is None:
            return self.async_abort(reason="no_supported_devices")

        errors: dict[str, str] = {}
        defaults = {
            CONF_NAME: self._cloud_device.name or DEFAULT_NAME,
            CONF_HOST: self._discovered_host,
            **(user_input or {}),
        }
        if user_input is not None:
            data = {
                CONF_NAME: user_input[CONF_NAME],
                CONF_HOST: user_input[CONF_HOST],
                CONF_DEVICE_ID: self._cloud_device.device_id,
                CONF_LOCAL_KEY: self._cloud_device.local_key,
            }
            try:
                return await self._async_create_validated_entry(data)
            except InvalidLocalKey:
                errors["base"] = "invalid_key"
            except EmerioCommunicationError:
                errors["base"] = "cannot_connect"
            except Exception:  # pragma: no cover - defensive HA boundary
                _LOGGER.exception("Unexpected error probing the Emerio device")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="cloud_device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=defaults[CONF_NAME]): str,
                    vol.Required(CONF_HOST, default=defaults[CONF_HOST]): str,
                }
            ),
            errors=errors,
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure local credentials manually."""

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                return await self._async_create_validated_entry(user_input)
            except InvalidLocalKey:
                errors["base"] = "invalid_key"
            except EmerioCommunicationError:
                errors["base"] = "cannot_connect"
            except Exception:  # pragma: no cover - defensive HA boundary
                _LOGGER.exception("Unexpected error probing the Emerio device")
                errors["base"] = "unknown"

        defaults = user_input or {}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME)
                ): str,
                vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
                vol.Required(
                    CONF_DEVICE_ID, default=defaults.get(CONF_DEVICE_ID, "")
                ): str,
                vol.Required(
                    CONF_LOCAL_KEY, default=defaults.get(CONF_LOCAL_KEY, "")
                ): str,
            }
        )
        return self.async_show_form(step_id="manual", data_schema=schema, errors=errors)

    async def _async_create_validated_entry(self, data: dict[str, Any]) -> FlowResult:
        """Validate local access and create the config entry."""

        await _async_validate_input(self.hass, data)
        await self.async_set_unique_id(data[CONF_DEVICE_ID])
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=data[CONF_NAME], data=data)

    def _show_scan_form(
        self,
        errors: dict[str, str] | None = None,
        placeholders: dict[str, str] | None = None,
    ) -> FlowResult:
        """Show the active Tuya QR token."""

        assert self._cloud is not None
        assert self._cloud.qr_code is not None
        return self.async_show_form(
            step_id="scan",
            data_schema=vol.Schema(
                {
                    vol.Optional("QR"): QrCodeSelector(
                        config=QrCodeSelectorConfig(
                            data=f"tuyaSmart--qrLogin?token={self._cloud.qr_code}",
                            scale=5,
                            error_correction_level=QrErrorCorrectionLevel.QUARTILE,
                        )
                    )
                }
            ),
            errors=errors or {},
            description_placeholders=placeholders or {},
        )


async def _async_validate_input(hass: HomeAssistant, data: dict[str, Any]) -> None:
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


def _discover_device_sync(device_id: str) -> str:
    """Return the LAN address found by TinyTuya, if any."""

    result = tinytuya.find_device(dev_id=device_id) or {}
    host = result.get("ip")
    return str(host) if host else ""
