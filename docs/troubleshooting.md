# Troubleshooting

**Invalid authentication.** The key id or secret was rejected, or the
signature did not match. Create a new key in SolisCloud and use
reauthentication. A portal password is not accepted. The log does not contain
the secret. Diagnostics redact the key id, the secret and the config-entry
unique id.

**Local logger is pausing reads.** The repair `logger_locked` means the stick accepted TCP and then reset the session. The integration keeps the last values (the data source reads `degraded`) and retries after at least 10 seconds, growing to about 10 minutes. Power-cycling the inverter, or waiting, clears it. A repair opens after 10 minutes.

**S5-WiFi-ST port 502 closed.** The repair `logger_s5_closed` means the S5 stick does not listen on 502, which its firmware often does. Use a Modbus TCP gateway on the inverter RS485 port, or SolisCloud only.

**Control not migrated.** The repair `legacy_control` says the upstream integration's control settings were not carried over. Nothing on the inverter changed. The repair stays until you dismiss it.

**No inverters.** The repair `no_inverters` has the same cause as "No plants" below.

**Clock.** SolisCloud rejects requests whose `Date` header is more than 15
minutes from UTC. If the response includes a Date header, the client signs
the next attempt with that clock. If it still fails, set the Home Assistant
host clock. The repair is `clock_skew`.

**No plants.** The key can sign requests but the account returned no
stations. A plant without an id looks the same. Rename the plant in
SolisCloud so an id is generated, which is the same workaround the upstream
README documents.

**Abnormal data.** SolisCloud answered with code `1`. That is an account-side
fault. Disabling and recreating the API key, or a Solis ticket, is what
clears it.

**SolisCloud is unreachable.** The repair `cloud_outage` opens after three
failed polls, or immediately when the last good snapshot is already more than
15 minutes old. Live readings are hidden. Energy totals stay put. The repair
clears itself on the next successful poll. A single blip does not open it and
does not start reauthentication. An HTML 403 from a gateway is this case, not
a bad key.

**Power unavailable while energy is still there.** The inverter's
`dataTimestamp` is older than 15 minutes, or that inverter's detail call
failed while the rest of the account was readable. Energy totals keep the
last value. Status reads `stale`. A restart during an outage loads those
totals again from the last snapshot and from RestoreSensor.

**Power is 0.** The inverter state is offline and the sample is fresh. On a
local logger that is also what you see at night: the data-source sensor
reads `asleep`, energy totals stay, and no repair is opened.

**Discovery found nothing.** The inverter has to be producing. The stick is
powered by the inverter, so a night-time setup cannot see it. Run the flow
again after sunrise, or type the address and the logger serial. An S2-WL-ST
never appears in that UDP list. Use the IP from SolisCloud.

**Port 502 is closed.** The repair `logger_tcp_disabled` means the S2-WL-ST
is on the network and Modbus TCP is not listening. Firmware before
`100101B4` has no Modbus TCP. Some later updates close the port. Ask Solis
for a build that keeps it open. Nothing in Home Assistant can open a port
the stick has shut.

**Port 502 accepts TCP and then stays silent.** The repair
`logger_modbus_silent` means another client already holds the stick's one
connection, often SolisCloud on firmware that cannot do both. The official
note is that enabling TCP disconnects SolisCloud. Firmware `100121cf`,
`100121d0` and `100141dx` are the releases reported to allow both. Do not
run a second Modbus integration against the same stick.

**The logger changed address.** DHCP can hand the stick a new lease after it
wakes. The integration finds a Solarman stick by serial (UDP 48899) or by
MAC prefix `BC:54:F9` and updates the existing entry. One info log names the
new address. You do not add the stick again. An S2, an S5 and a gateway are
not in that broadcast. Reserve the address, or change it with Reconfigure.

**Gateway refused the port.** The repair `logger_gateway` means nothing
accepted TCP. Check the IP and the port. This is not the inverter switching
a Solis stick off. A gateway powered from the inverter's USB port goes quiet
at night and this repair is what opens after 10 minutes.

**Wrong framing.** The repair `logger_wrong_protocol` means the bytes were
not the kind stored on the entry. Transparent mode is Modbus RTU over TCP.
Modbus conversion mode is the Modbus TCP gateway. A value off by 256 is the
same mistake. The repair also covers a reply from the wrong slave and a
device that is not serving the Solis register map.

**Web page stick says the password was rejected.** The repair
`logger_web` and the form error mean port 80 answered 401. The user is
`admin`. The factory password is `123456789`; some firmware replaces it with
your Wi-Fi password. Fix it with Reconfigure on the Solis entry. A page that
loads but is not the Solis inverter page gives the same repair: open the
stick address in a browser and check it shows the inverter status.

**Web page values move every five minutes.** That is the stick, not the
integration. It polls the inverter on a five-minute cycle and serves the last
sample until the next one. For live data use a gateway on the inverter RS485
port.

**S4, S6 and 4G sticks.** There is no local port to type. Use
SolisCloud, or wire a gateway to the inverter RS485 port and pick Modbus
TCP gateway. Details and sources are in
[docs/research/dongles.md](research/dongles.md).

**Energy dropped to 0 in the middle of the day.** A null or zero sample is
held at the last good value for that period. If the number still falls, the
cloud sent a lower reading after the plant-local period rolled. In British
Summer Time that used to show up around 23:00 when the plant offset stayed on
winter time (hultenvp/solis-sensor issue 533). Leave the plant timezone
empty so Home Assistant's zone is used when it is within an hour of the
Solis offset, or set `Europe/London` (or your zone) explicitly. `plant`
forces the numeric offset.

**Two Solis integrations.** `hultenvp/solis-sensor` and this rewrite share the
`solis` folder. Only one can be installed. Remove the other and restart.

**Duplicate entities after migration.** Reload the entry. Migration runs on
every poll and keeps the legacy entity id when both the old and the new
unique id exist. An inverter that was offline at the first restart is
migrated when it next appears.
