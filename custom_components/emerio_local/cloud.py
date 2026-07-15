"""Short-lived Tuya cloud session used only during onboarding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tuya_sharing import LoginControl, Manager

from .const import TUYA_CLIENT_ID, TUYA_SCHEMA

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


@dataclass(frozen=True, slots=True)
class TuyaCloudDevice:
    """Device credentials returned by Tuya's device-sharing API."""

    device_id: str
    local_key: str
    name: str
    product_id: str
    product_name: str
    online: bool


class TuyaCloudError(Exception):
    """A safe, user-facing Tuya cloud error."""

    def __init__(self, code: str | int, message: str) -> None:
        super().__init__(f"Tuya cloud error {code}: {message}")
        self.code = str(code)
        self.message = message

    @property
    def placeholders(self) -> dict[str, str]:
        """Return config-flow placeholders without credentials or tokens."""

        return {"code": self.code, "msg": self.message}


class TuyaCloudSession:
    """Authenticate once and discard all cloud tokens after device discovery."""

    def __init__(self) -> None:
        self._login_control = LoginControl()
        self._user_code: str | None = None
        self._qr_code: str | None = None

    @property
    def user_code(self) -> str | None:
        """Return the user code while the config flow is active."""

        return self._user_code

    @property
    def qr_code(self) -> str | None:
        """Return the current QR token while the config flow is active."""

        return self._qr_code

    async def async_request_qr_code(self, hass: HomeAssistant, user_code: str) -> str:
        """Request a QR token for a Smart Life user code."""

        try:
            response = await hass.async_add_executor_job(
                self._login_control.qr_code,
                TUYA_CLIENT_ID,
                TUYA_SCHEMA,
                user_code,
            )
        except Exception as err:
            # Request exceptions can contain the full URL, including user code.
            raise TuyaCloudError("connection", "Unable to contact Tuya") from err
        if not response.get("success", False):
            raise _cloud_error(response)

        qr_code = response.get("result", {}).get("qrcode")
        if not isinstance(qr_code, str) or not qr_code:
            raise TuyaCloudError("invalid_response", "QR code is missing")

        self._user_code = user_code
        self._qr_code = qr_code
        return qr_code

    async def async_login_and_get_devices(
        self, hass: HomeAssistant
    ) -> tuple[TuyaCloudDevice, ...]:
        """Finish QR login and return devices with local credentials."""

        if self._user_code is None or self._qr_code is None:
            raise TuyaCloudError("not_ready", "QR login has not been started")

        try:
            success, info = await hass.async_add_executor_job(
                self._login_control.login_result,
                self._qr_code,
                TUYA_CLIENT_ID,
                self._user_code,
            )
        except Exception as err:
            # Request exceptions can contain the QR token and user code in the URL.
            raise TuyaCloudError("connection", "Unable to contact Tuya") from err
        if not success:
            raise _cloud_error(info)

        try:
            devices = await hass.async_add_executor_job(
                _get_devices,
                self._user_code,
                info,
            )
        except TuyaCloudError:
            raise
        except Exception as err:
            raise TuyaCloudError("connection", "Unable to read Tuya devices") from err

        # Tokens, QR token and user code are only needed during this flow.
        # The resulting Home Assistant entry stores local credentials only.
        self.clear()
        return devices

    def clear(self) -> None:
        """Forget all temporary login material."""

        self._user_code = None
        self._qr_code = None


def _get_devices(
    user_code: str, login_info: dict[str, Any]
) -> tuple[TuyaCloudDevice, ...]:
    """Fetch devices synchronously through the Tuya sharing SDK."""

    required = (
        "terminal_id",
        "endpoint",
        "t",
        "uid",
        "expire_time",
        "access_token",
        "refresh_token",
    )
    missing = [key for key in required if key not in login_info]
    if missing:
        raise TuyaCloudError("invalid_response", "Login response is incomplete")

    token_info = {
        "t": login_info["t"],
        "uid": login_info["uid"],
        "expire_time": login_info["expire_time"],
        "access_token": login_info["access_token"],
        "refresh_token": login_info["refresh_token"],
    }
    manager = Manager(
        TUYA_CLIENT_ID,
        user_code,
        login_info["terminal_id"],
        login_info["endpoint"],
        token_info,
        None,
    )
    manager.update_device_cache()

    devices = []
    for device in manager.device_map.values():
        device_id = getattr(device, "id", "")
        local_key = getattr(device, "local_key", "")
        if not device_id or not local_key:
            continue
        devices.append(
            TuyaCloudDevice(
                device_id=str(device_id),
                local_key=str(local_key),
                name=str(getattr(device, "name", "") or "Tuya device"),
                product_id=str(getattr(device, "product_id", "") or ""),
                product_name=str(getattr(device, "product_name", "") or ""),
                online=bool(getattr(device, "online", False)),
            )
        )

    return tuple(devices)


def _cloud_error(response: dict[str, Any] | None) -> TuyaCloudError:
    """Build a sanitized exception from a Tuya response."""

    response = response or {}
    return TuyaCloudError(
        response.get("code", "unknown"),
        str(response.get("msg", "Unknown error")),
    )
