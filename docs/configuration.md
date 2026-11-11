# Configuration

| Field | Where | Meaning |
| --- | --- | --- |
| Connection | Entry | `cloud`, `local`, or `both`. Cloud is the default. `local` is the Solarman logger. `both` reads the logger first and lets SolisCloud fill energy totals the logger left empty. |
| API URL | Entry | SolisCloud host, including the port. Public http is upgraded to https. |
| Logger address | Entry | Current IP of the stick. Discovery fills it in. A later DHCP lease is written back into this field. |
| Logger serial | Entry | Solarman serial. This, with the MAC, is the identity of the stick. The config-entry unique id for a local or fallback entry is this serial. |
| API key ID | Entry | Key id from API management. Also the config-entry unique id. Diagnostics redact it. |
| API key secret | Entry | Key secret. Stored in the config entry. Diagnostics redact it. It is not written to the log. |
| Plants | Entry | Which stations to poll. Reconfigure to change the set. |
| Poll interval | Options | Seconds between polls. Cloud default 300, and a cloud-only entry never polls faster than 60. A local logger defaults to 60 and cannot be faster than 30. Maximum 3600. |
| Inverter serial | Entry | Optional SolisCloud serial, for example `110E40182280017`. Local and cloud entities use it. Leave it empty to read it from the stick when that range answers, or from SolisCloud in `both` mode. |
| Request timeout | Options | Seconds to wait for one HTTP call. Default 30. Minimum 5. |
| Minimum gap per endpoint | Options | Seconds between calls to the same path. Default 2. Minimum 0.5, which is the documented 2 calls per second ceiling. |
| Plant timezone | Options | Empty follows Home Assistant when that zone is within an hour of the plant offset. An IANA name such as `Europe/London` forces that zone. `plant` keeps the numeric offset Solis sent. |

Reauthentication replaces the key id and secret and refuses a key that
belongs to a different account. Reconfigure changes the URL, the plant
selection, the polling numbers and the timezone. A cloud entry can gain a
logger from the same form. Saving options does not drop the timeout or the
timezone.

## Local logger

Choose `local` or `both` on the first step. The inverter has to be
producing: the stick is powered by the inverter, and at night it disappears
from the LAN. Discovery listens for the IGEN UDP 48899 reply and fills in
the address and the serial. Type the address and the serial yourself when
discovery finds nothing and you already know them. Reserve that address in
the router's DHCP settings. Do not configure a static address on the stick.
The connection test picks the Modbus slave (1, then 2, then 3), V5 or plain
Modbus TCP, and the register map. It reads the 3000-range map first and does
not touch 33000 once that map answers.

The same stick cannot be added twice. DHCP on MAC prefix `BC:54:F9`, and a
failed connect that is followed by a discovery reply with the same serial,
update the stored address. The log line is `Solarman logger <serial> is now
at <ip>`.

`both` uses the stick for live readings. Cloud numbers are used only for
energy totals the stick left empty, and for the series name and model code
when the stick does not carry them. A stick that is off for the night stays
on the last local totals and publishes live power as 0. The dashboard stays
on that asleep sample. A stick that accepts TCP and then resets the session
is not asleep: live readings come from a fresh SolisCloud sample, the data
source reads `degraded`, and local is tried again after a pause of at least
10 seconds that grows to about 10 minutes. A few tens of watts in the morning
is production.

The same API key cannot be added twice.
