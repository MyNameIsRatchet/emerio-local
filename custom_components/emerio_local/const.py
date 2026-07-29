"""Constants for the Emerio Local integration."""

from homeassistant.const import Platform

DOMAIN = "emerio_local"

CONF_DEVICE_ID = "device_id"
CONF_LOCAL_KEY = "local_key"
CONF_POWER_SENSOR = "power_sensor"
CONF_POWER_ON_THRESHOLD = "power_on_threshold"
CONF_COMPRESSOR_THRESHOLD = "compressor_threshold"
CONF_SETUP_METHOD = "setup_method"
CONF_USER_CODE = "user_code"

SETUP_METHOD_CLOUD = "cloud"
SETUP_METHOD_MANUAL = "manual"

SUPPORTED_PRODUCT_IDS = {"bvgvah9atllpyt5s"}

# Public Home Assistant application identity used by the official Tuya
# integration for Smart Life device-sharing authorization.
TUYA_CLIENT_ID = "HA_3y9q4ak7g4ephrvke"
TUYA_SCHEMA = "haauthorize"

DEFAULT_NAME = "Emerio Klimaanlage"
DEFAULT_POWER_ON_THRESHOLD = 10.0
DEFAULT_COMPRESSOR_THRESHOLD = 300.0
PROTOCOL_VERSION = 3.4
TUYA_PORT = 6668

PLATFORMS: list[Platform] = [
    Platform.CLIMATE,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.BUTTON,
]
