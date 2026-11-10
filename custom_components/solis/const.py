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
ISSUE_LEGACY_CONTROL = "legacy_control"
