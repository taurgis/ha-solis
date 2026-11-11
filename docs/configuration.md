# Configuration

| Field | Where | Meaning |
| --- | --- | --- |
| Connection | Entry | `cloud`, `local`, or `both`. Cloud is the default. `local` is a data logger. `both` reads the logger first and lets SolisCloud fill energy totals the logger left empty. |
| API URL | Entry | SolisCloud host, including the port. Public http is upgraded to https. |
| Logger | Entry | `auto`, `solarman` (DLS-L / LSW, V5 on 8899), or `s2_wl_st` (Solis S2-WL-ST, Modbus TCP on 502). Auto-detect probes 502, then 8899. |
| Logger address | Entry | Current IP of the stick. Solarman discovery fills it in. A later DHCP lease is written back when the MAC is known. |
| Logger port | Entry | Empty uses 502 or 8899 for the detected stick. Set it only to override. |
| Modbus slave | Entry | Empty tries 1, then 2, then 3. The Solis default is 1. |
| Logger serial | Entry | Solarman: the numeric serial. That is the config-entry unique id. S2-WL-ST: the serial in the Wi-Fi name `D_` or `Solis_`. The unique id is `s2:` plus that serial, or `s2:` plus the inverter serial when the stick serial was not typed. |
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
from the LAN. Leave Logger on Auto-detect. The flow tries Modbus TCP port
502 (Solis S2-WL-ST), then Solarman V5 on 8899. Discovery listens for the
IGEN UDP 48899 reply and fills in a Solarman address and serial. An
S2-WL-ST does not answer that broadcast. Type its IP from the SolisCloud
logger page. The serial on that stick is the suffix of the setup network
`D_<serial>` or `Solis_<serial>`.

Reserve that address in the router's DHCP settings. Do not configure a
static address on the stick. The connection test picks the Modbus slave
(1, then 2, then 3) unless you type one, V5 or plain Modbus TCP, and the
register map. It reads the 3000-range map first and does not touch 33000
once that map answers. An S2-WL-ST is read with function 4, and with
function 3 if the stick returns illegal function. The official Modbus Poll
demo uses function 3 at address 3000.

The same stick cannot be added twice. DHCP on MAC prefix `BC:54:F9`, and a
failed connect that is followed by a discovery reply with the same serial,
update a Solarman address. The log line is `Solarman logger <serial> is now
at <ip>`. An S2-WL-ST has no published MAC OUI, so DHCP updates it only when
a MAC was stored on the entry. Give it a reservation. The log line is
`S2-WL-ST <serial> is now at <ip>`.

Port 502 closed, or a socket that accepts TCP and sends no Modbus, is not
treated as night. In `both` mode a fresh cloud sample is published and,
after 10 minutes, a repair explains the firmware. See
[S2-WL-ST](research/s2-wl-st.md).

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
