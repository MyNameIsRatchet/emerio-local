"""Constants for the Emerio Local integration."""

from homeassistant.const import Platform

DOMAIN = "emerio_local"

CONF_DEVICE_ID = "device_id"
CONF_LOCAL_KEY = "local_key"

DEFAULT_NAME = "Emerio Klimaanlage"
PROTOCOL_VERSION = 3.4
TUYA_PORT = 6668

PLATFORMS: list[Platform] = [
    Platform.CLIMATE,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.BUTTON,
]
