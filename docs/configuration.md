# Configuration

| Field | Where | Meaning |
| --- | --- | --- |
| API URL | Entry | SolisCloud host, including the port. |
| API key ID | Entry | Key id from API management. |
| API key secret | Entry | Key secret. Stored in the config entry. Diagnostics redact it. |
| Plants | Entry | Which stations to poll. Reconfigure to change the set. |
| Poll interval | Options | Seconds between polls. Default 300. Minimum 60. |
| Request timeout | Options | Seconds to wait for one HTTP call. Default 30. |
| Minimum gap per endpoint | Options | Seconds between calls to the same path. Default 2. |

Reauthentication replaces the key id and secret. Reconfigure changes the URL,
the plant selection and the three polling numbers. Saving options does not
drop the timeout.
