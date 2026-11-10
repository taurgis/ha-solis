# Configuration

| Field | Where | Meaning |
| --- | --- | --- |
| API URL | Entry | SolisCloud host, including the port. Public http is upgraded to https. |
| API key ID | Entry | Key id from API management. Also the config-entry unique id. Diagnostics redact it. |
| API key secret | Entry | Key secret. Stored in the config entry. Diagnostics redact it. It is not written to the log. |
| Plants | Entry | Which stations to poll. Reconfigure to change the set. |
| Poll interval | Options | Seconds between polls. Default 300. Minimum 60. Maximum 3600. |
| Request timeout | Options | Seconds to wait for one HTTP call. Default 30. Minimum 5. |
| Minimum gap per endpoint | Options | Seconds between calls to the same path. Default 2. Minimum 0.5, which is the documented 2 calls per second ceiling. |
| Plant timezone | Options | Empty follows Home Assistant when that zone is within an hour of the plant offset. An IANA name such as `Europe/London` forces that zone. `plant` keeps the numeric offset Solis sent. |

Reauthentication replaces the key id and secret and refuses a key that
belongs to a different account. Reconfigure changes the URL, the plant
selection, the polling numbers and the timezone. Saving options does not
drop the timeout or the timezone.

The same API key cannot be added twice.
