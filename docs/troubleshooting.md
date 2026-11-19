# Troubleshooting

Each problem below has its own heading, so you can link to it. Repairs that
this integration opens link to the matching section. Repairs are never
"fixable" from the Repairs panel: the fix is a step outside Home Assistant,
and the repair clears itself on the next poll that works. Repairs stay open
while the entry is unloaded or reloaded, and are removed when you delete the
entry.

## SolisCloud

### SolisCloud rejected the API key

The key ID or secret was rejected, or the signature did not match. Home
Assistant shows a **Reauthenticate** card on the Solis entry and stops
polling with that key. Create a new key in SolisCloud (Account → Basic
settings → API management) and enter it in the reauthentication dialog. A
portal password is not accepted. The key must belong to the same SolisCloud
account; a key from another account is refused.

On a local entry with SolisCloud backup, the stick keeps being read and the
same reauthentication card asks for the new key.

The log never contains the secret. Diagnostics redact the key ID, the
secret and the config-entry unique ID.

### Clock outside the SolisCloud window

Repair `clock_skew`. SolisCloud rejects requests whose `Date` header is more
than 15 minutes from UTC. When the reply carries the server's own time, the
client signs the next attempt with that clock. If it still fails, fix the
clock and time zone of the Home Assistant host. The repair clears on the next
poll that works.

### SolisCloud returned no inverters

Repair `no_inverters`. The plant list or inverter list came back empty.
Check in SolisCloud that the plant still exists, still has an inverter, and
is visible to the account that owns the API key. Use Reconfigure → Plants
on the Solis entry to choose the plants again. The last inverter list is
kept for a couple of polls before an empty one is believed.

### No plants during setup

The key signs requests but the account returned no plants. A plant without
an ID looks the same. Rename the plant in SolisCloud so an ID is generated,
which is the same workaround the upstream README documents.

### Abnormal data

SolisCloud answered with code `1`. That is a fault on the account side, not
something Home Assistant can fix, so no repair is opened: the update fails,
live sensors go unavailable, and the failure is logged once. Disabling and
recreating the API key, or a ticket with Solis, is what clears it.

### SolisCloud is unreachable

A single failed poll keeps showing the last values. After three failed polls,
or as soon as the last good sample is more than 15 minutes old, live
readings become unavailable and energy totals stay where they were. One
warning is logged when that starts and one info line when SolisCloud is back.
No repair is opened: nothing in Home Assistant can fix an outage. An HTML 403
page from a gateway is this case, not a bad key.

## Values

### Power is unavailable while energy is still there

The inverter's measurement time is older than 15 minutes, or that inverter's
detail call failed while the rest of the account was readable. Energy totals
keep the last value and Status reads `stale`. A restart during an outage
loads those totals again from the last snapshot.

### Power is 0 and grid power or house consumption is unknown

The inverter is offline (Status reads `offline`). What it produces is 0;
what it only measures, such as grid power, house consumption, grid voltage
and temperature, reads unknown because nothing measures it while the
inverter is off. With a local stick that is also what you see at night:
Data source reads `asleep`, energy totals stay, and no repair is opened.
Automations that need a number can use the energy totals, or treat unknown
as "no reading".

### Energy dropped to 0 in the middle of the day

A null or zero sample is held at the last good value for that period. If the
number still falls, the cloud sent a lower reading after the plant's day
rolled over. In British Summer Time that used to show up around 23:00 when
the plant offset stayed on winter time (hultenvp/solis-sensor issue 533).
Leave **Plant timezone** on Automatic, or pick your zone (for example
`Europe/London`) in the options. **Plant offset from Solis** forces the
numeric offset.

### Grid power has the wrong sign in the Energy dashboard

SolisCloud reports grid power positive while exporting and negative while
importing. Home Assistant expects the opposite. In the Energy dashboard power
settings, choose the inverted option for the grid power sensor.

### Web page values change only every five minutes

That is the stick, not the integration. It reads the inverter on a
five-minute cycle and serves the last sample until the next one. For live
data use an RS485 gateway on the inverter.

## Local stick

### Discovery found nothing

The inverter has to be producing. The stick is powered by the inverter, so a
night-time setup cannot see it. Run the flow again after sunrise, or type
the address. Only Solarman / IGEN sticks answer the discovery broadcast. An
S2-WL-ST, an S5, a web-page stick and an RS485 gateway never appear in that
list: use the IP from the SolisCloud logger details page or from your router.

### The stick changed address

A new DHCP lease can move the stick. A Solarman / IGEN stick is recognised
by its serial and MAC (prefix `E8:FD:F8` or `BC:54:F9`) and the existing
entry is updated; one info line in the log names the new address. Any other
stick with a stored MAC is followed the same way. A web-page stick is never
moved by DHCP, because the entry sends its password to that address. For
those, and for sticks without a stored MAC, reserve the address in your
router, or change it with Reconfigure → Local stick.

### Local logger is pausing reads

Repair `logger_locked`. The stick accepted the connection and then reset it.
On a DLS-L this follows too many sessions or a read of a register the
inverter does not have. On an S2-WL-ST the stick often allows only one Modbus
client. The integration keeps the last values (Data source reads `degraded`),
uses SolisCloud if it is the backup, and tries the stick again after at least
10 seconds, then less often, up to about 10 minutes. The repair opens only
after 10 minutes of this. Waiting, or power-cycling the inverter, clears it.

### S2-WL-ST port 502 is closed

Repair `logger_tcp_disabled`, or the setup error that port 502 is closed. The
S2-WL-ST is on the network but Modbus TCP is not listening. Nothing in Home
Assistant can open a port the stick has shut.

S2-WL-ST firmware reports:

| Firmware | What is reported |
| --- | --- |
| before `100101B4` | No Modbus TCP at all. |
| `100101B4` | First build with Modbus TCP. |
| `100121b4`, `100121cf`, `100121d0` | Modbus TCP and SolisCloud reported working together. |
| `100141dx` (for example `100141d2`, `100141d9`) | Reported to allow both, but some allow only one local client. `100141d9` has been seen leaving port 502 open but not answering generic clients. |
| `1001318c` | Closing a connection can block port 502 for several minutes. |
| Some later updates | Port 502 closed again. |

Solis's official guide says enabling Modbus TCP disconnects the stick from
SolisCloud. Ask Solis support for a build that keeps port 502 open and still
uploads. Sources are in [the S2-WL-ST notes](research/s2-wl-st.md#firmware).

### Port 502 accepts the connection but sends nothing

Repair `logger_modbus_silent`, or the matching setup error. Another client
already holds the stick's one connection. On older S2-WL-ST firmware that is
often SolisCloud itself. Stop any other Modbus integration or tool talking to
the same stick, or ask Solis for one of the builds in the table above. An
S5-WiFi-ST that still has port 502 open has the same limit.

### Logger gateway

Repair `logger_gateway`: the Modbus gateway is not accepting connections.
Nothing accepted the connection on that address and port. Check the IP, the
port (502 for Modbus TCP mode; 8899, 4196, 26 or 23 for transparent mode)
and that the gateway is powered. Give the gateway a DHCP reservation. If its
address has moved, use Reconfigure → Local stick and type the new one.

This is not the inverter switching a Solis stick off for the night. A
gateway powered from the inverter's USB port does go quiet at night, and
this repair is what opens after 10 minutes.

### Wrong protocol

Repair `logger_wrong_protocol`: the logger framing does not match. The
device answered, but not in the format stored on the entry. A Waveshare,
Elfin or USR gateway in transparent mode needs **RS485 gateway in
transparent mode**. The same device in Modbus TCP conversion mode needs
**RS485 gateway in Modbus TCP mode**. Values that are off by a factor of 256
are the same mistake. The repair also covers a reply from the wrong Modbus
slave (set the slave under Advanced options; Solis uses 1) and a device that
is not serving Solis inverter data (check the RS485 wiring). Change the type
or port with Reconfigure → Local stick.

### S5 port closed

Repair `logger_s5_closed`: the S5-WiFi-ST stopped serving Modbus TCP. Solis
does not officially support local access on the S5-WiFi-ST, and newer
firmware closes port 502 and the web page while SolisCloud keeps working.
Nothing in Home Assistant can reopen the port. Use Reconfigure → Local stick
to switch this entry to an RS485 gateway on the inverter. To use SolisCloud
instead, add your account with Reconfigure → SolisCloud account, then choose
SolisCloud only under Reconfigure → Connection.

### Web page stick asks for its password again

The stick's status page answered `401` three times in a row, or for 10
minutes, so it rejects the saved password. A single `401` (a stick that is
rebooting) only holds the last values. Home Assistant then shows a
**Reauthenticate** card on the Solis entry; enter the web password there. The
user is always `admin`. Leave the field empty to try the factory passwords
`123456789` and `admin` (sticks made by IGEN/Solarman for Solis, with the
`status.html` page, ship with `admin`). Some firmware replaces it with your
Wi-Fi password. With SolisCloud as backup, SolisCloud keeps the sensors going
until you enter the new password.

### Web page stick is not returning inverter data

Repair `logger_web`. The stick's address answered, but not with a Solis
status page (`inverter.cgi` or `status.html`). Open the stick address in a
browser and check that the inverter status page loads. A stick that is off
with the inverter at night is not this fault.

### S4, S6 and 4G sticks

No local port is known on these sticks, so there is nothing to type. If you
find one open, please open an issue with the stick model and firmware. Use
SolisCloud, or wire an RS485 gateway to
the inverter and pick **RS485 gateway in Modbus TCP mode**. Details and
sources are in [Dongles and gateways](research/dongles.md).

## Migration

### Legacy control

Repair `legacy_control`: inverter control was not migrated. The upstream
integration had the control API enabled. This integration does not send
`/v2/api/control` or `/v2/api/atRead`, because polling those calls has been
reported to drop the data logger's cloud session. Nothing on the inverter
changed: battery schedules already stored there keep running. The repair is
for information only; dismiss it once you have read it. It survives a
restart and is removed with the entry.

### Two Solis integrations

`hultenvp/solis-sensor` and this rewrite share the `solis` folder. Only one
can be installed. Remove the other and restart.

### Duplicate entities after migration

Reload the entry. Migration keeps the legacy entity ID when both the old and
the new unique ID exist. An inverter that was offline at the first restart
is migrated when it next appears.
