"""Constants for the Solis integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "solis"

CONF_API_URL = "api_url"
CONF_KEY_ID = "key_id"
CONF_KEY_SECRET = "key_secret"
CONF_STATION_IDS = "station_ids"
CONF_LEGACY_NAME = "legacy_name"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_REQUEST_TIMEOUT = "request_timeout"
CONF_ENDPOINT_INTERVAL = "endpoint_interval"
CONF_TIME_ZONE = "time_zone"
CONF_SOURCE = "source"
CONF_LOGGER_HOST = "logger_host"
CONF_LOGGER_SERIAL = "logger_serial"
CONF_LOGGER_MAC = "logger_mac"
CONF_SLAVE_ID = "slave_id"
CONF_REGISTER_MAP = "register_map"
CONF_INVERTER_SERIAL = "inverter_serial"

SOURCE_CLOUD = "cloud"
SOURCE_LOCAL = "local"
SOURCE_BOTH = "both"
REGISTER_STRING = "string"
REGISTER_HYBRID = "hybrid"

# IGEN / Solarman Wi-Fi sticks. The user's stick answered discovery as
# bc:54:f9:f5:7b:5c.
SOLARMAN_OUI = "bc:54:f9"

DEFAULT_API_URL = "https://www.soliscloud.com:13333"
DEFAULT_SCAN_INTERVAL = 300
DEFAULT_REQUEST_TIMEOUT = 30
DEFAULT_ENDPOINT_INTERVAL = 2.0

# Upstream solis-sensor 4.x stored the portal on this host. Kept so a migrated
# entry still reaches the same API the account was created against.
LEGACY_API_URL = "https://v3.soliscloud.com:13333"

MANUFACTURER = "Solis"

STALE_AFTER = timedelta(minutes=15)

ISSUE_AUTH = "invalid_auth"
ISSUE_CLOCK = "clock_skew"
ISSUE_NO_INVERTERS = "no_inverters"
ISSUE_ABNORMAL = "abnormal_data"
ISSUE_OUTAGE = "cloud_outage"
ISSUE_LEGACY_CONTROL = "legacy_control"

# Polls that fail this many times in a row, or a snapshot older than
# STALE_AFTER, are a sustained outage rather than a blip.
OUTAGE_POLLS = 3

# Repairs opened by a poll. Unload deletes these. Legacy control is not one
# of them: it records a migration choice and stays until the user dismisses it.
POLL_REPAIRS = (
    ISSUE_AUTH,
    ISSUE_CLOCK,
    ISSUE_NO_INVERTERS,
    ISSUE_ABNORMAL,
    ISSUE_OUTAGE,
)
