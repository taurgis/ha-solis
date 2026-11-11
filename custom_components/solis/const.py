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
CONF_LOGGER_PROTOCOL = "logger_protocol"
CONF_SERIAL_UNSUPPORTED = "serial_unsupported"
CONF_LOGGER_KIND = "logger_kind"
CONF_LOGGER_PORT = "logger_port"
CONF_LOGGER_FUNCTION = "logger_function"
CONF_LOGGER_DETECT = "logger_detect"
CONF_LOGGER_PRESET = "logger_preset"

SOURCE_CLOUD = "cloud"
SOURCE_LOCAL = "local"
SOURCE_BOTH = "both"
DATA_DEGRADED = "degraded"
REGISTER_STRING = "string"
REGISTER_HYBRID = "hybrid"
PROTOCOL_V5 = "v5"
PROTOCOL_MBTCP = "mbtcp"
PROTOCOL_MBTCP_ALT = "mbtcp30003"
PROTOCOL_RTU = "rtu"
MBTCP_ALT_PORT = 30003
SOLARMAN_PORT = 8899
S2_PORT = 502
RTU_PORT = 8899
LOGGER_AUTO = "auto"
LOGGER_SOLARMAN = "solarman"
LOGGER_S2 = "s2_wl_st"
LOGGER_S5 = "s5_wifi_st"
LOGGER_MBTCP = "modbus_tcp"
LOGGER_RTU = "rtu_tcp"
REASON_LOCKED = "locked"
REASON_TCP_DISABLED = "tcp_disabled"
REASON_SILENT = "silent"
REASON_TIMEOUT = "timeout"
REASON_CLOSED = "closed"
REASON_REFUSED = "refused"
REASON_GARBAGE = "garbage"
REASON_GATEWAY = "gateway_down"
REASON_WRONG_PROTOCOL = "wrong_protocol"
REASON_WRONG_SLAVE = "wrong_slave"
REASON_UNSUPPORTED = "unsupported_map"
REASON_S5_CLOSED = "s5_port_closed"
# The 4G string map that answered on this stick stops before 3050.
# 3050 and the 33000 hybrid block are what lock the local port.
REGISTER_SAFE_CEILING = 3050

# IGEN / Solarman Wi-Fi sticks. The user's stick answered discovery as
# bc:54:f9:f5:7b:5c.
SOLARMAN_OUI = "bc:54:f9"

DEFAULT_API_URL = "https://www.soliscloud.com:13333"
DEFAULT_SCAN_INTERVAL = 300
DEFAULT_LOCAL_SCAN_INTERVAL = 60
MIN_LOCAL_SCAN_INTERVAL = 30
MIN_CLOUD_SCAN_INTERVAL = 60
LOCAL_REQUEST_GAP = 1.0
LOCAL_TIMEOUT_CAP = 8.0
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
ISSUE_LOGGER_LOCKED = "logger_locked"
ISSUE_LOGGER_TCP = "logger_tcp_disabled"
ISSUE_LOGGER_SILENT = "logger_modbus_silent"
ISSUE_LOGGER_GATEWAY = "logger_gateway"
ISSUE_LOGGER_PROTOCOL = "logger_wrong_protocol"
ISSUE_LOGGER_S5 = "logger_s5_closed"

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
    ISSUE_LOGGER_LOCKED,
    ISSUE_LOGGER_TCP,
    ISSUE_LOGGER_SILENT,
    ISSUE_LOGGER_GATEWAY,
    ISSUE_LOGGER_PROTOCOL,
    ISSUE_LOGGER_S5,
)
