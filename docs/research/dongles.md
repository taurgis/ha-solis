# Solis-compatible data loggers

This note is the input for local transports beyond the two sticks already
in the tree. The maintainer does not own most of these devices. Nothing
below was checked on a bench for this change. Confidence is marked on each
row of the matrix. "Verified" means a vendor document, a protocol
implementation, or a user report that names the model and the port.
"Reported" means several independent users, without a vendor spec.
"Guess" means the behaviour is inferred from the chip family or from the
absence of a local port, and the integration does not pretend otherwise.

The register map does not change with the logger. String inverters use
the 3000-range function-4 map in
[solarman-local.md](solarman-local.md). Hybrids use the 33000 map. What
changes is the framing on the LAN, how the device is found, and what a
closed port means.

Two transports were already implemented and are the baseline:

- Solarman / IGEN V5, including plain Modbus TCP on the same port or on
  30003. See [solarman-local.md](solarman-local.md).
- Solis S2-WL-ST, Modbus TCP on port 502. See [s2-wl-st.md](s2-wl-st.md).

## How port 502 is named

A Solis register map on port 502 is not an S2-WL-ST by itself. A Waveshare, Elfin or USR in Modbus TCP mode returns the same inverter registers. The logger serial is not in those registers, and function 43 (MEI) is not used: a second Modbus session is how firmware `1001318c` wedges port 502 for minutes. The signal that does not touch port 502 is the device's own page on port 80.

| Signal | S2-WL-ST | Gateway | Confidence |
| --- | --- | --- | --- |
| HTTP page contains `s2-wl`, or `solis`/`ginlong` together with `logger`, `datalogger` or `lan settings` | Positive. Auto-detect stores `s2_wl_st`. | — | Verified that the S2 has a web page (Solis service article: `admin`, LAN Settings, and the [North America copy](https://usservice.solisinverters.com/support/solutions/articles/73000660897-modbus-tcp-ip)). The exact tokens are what that page and the HA report of a basic-auth status page make matchable. Several firmware builds have no page at all ([HA thread](https://community.home-assistant.io/t/getting-data-from-solis-inverter/302189?page=7), [solis_modbus discussion 379](https://github.com/Pho3niX90/solis_modbus/discussions/379)). An empty page is not a vote for the S2. |
| HTTP page contains `waveshare`, `elfin`, `ew11`, `ee11`, `hi-flying`, `hf-a11`, `usr-tcp`, `tcp232` | — | Positive. Auto-detect stores `modbus_tcp`. | Reported from vendor pages and the [Fibaro gateway guide](https://hdev.pl/doc/Modbus_Gateway_config_for_Fibaro_EN.pdf). Hi-Flying also builds Solarman sticks; those answer V5 on 8899, so a Hi-Flying page next to Modbus TCP on 502 is treated as a gateway. |
| Port 80 closed, empty, or unnamed | Inconclusive. The form asks, with S2-WL-ST pre-selected. | Same question. A gateway page that also says Solis is the same question. | The pre-selection is the device Solis documents on 502, not a classification. |
| MAC OUI | Not published for the S2. | Not matched. A wrong OUI would claim every device from that vendor. | No OUI was added. |
| Modbus FC43 / slave 0 or 255 | Not probed. | Not probed. | Considered and skipped. The inverter map is shared, and a second port 502 session is unsafe. |
| DHCP hostname `ew11`, `ee11`, `elfin`, `usr-tcp`, `usr-w610`, `usr-dr` | — | Does not classify a new entry. If an entry already uses that IP and has no MAC, the MAC is stored. A later lease with that MAC updates the host. | Reported hostnames: Elfin shows up as `EW11` ([HA thread](https://community.home-assistant.io/t/elfin-ew11-connection-issue/581610)), USR AP names are `USR-<model>_…` ([Fibaro guide](https://hdev.pl/doc/Modbus_Gateway_config_for_Fibaro_EN.pdf)). |

An existing S2 entry is not probed again at startup. Picking S2-WL-ST in the form skips the web check. Picking S5-WiFi-ST also skips it and stays experimental.

## RTU factory ports

Transparent mode is a different listen port on each vendor. Modbus conversion mode is the `modbus_tcp` kind and stays on 502, which is the port the Solis write-ups and the Fibaro guide tell people to set. The RTU kind uses the factory port only when Port is left empty.

| Preset | Port | Source |
| --- | --- | --- |
| Default, and Elfin EW11 / EE11 | 8899 | Hi-Flying factory socket. [Fibaro guide](https://hdev.pl/doc/Modbus_Gateway_config_for_Fibaro_EN.pdf): "default is 8899". This is the default preset because it is the transparent port most often left untouched on the gateways used with Solis. |
| Waveshare RS485 TO ETH | 4196 | [Fibaro guide](https://hdev.pl/doc/Modbus_Gateway_config_for_Fibaro_EN.pdf): "default is 4196 or 8899". 4196 is the classic Waveshare TCP server port. Newer "B" units are often moved to 502 when Modbus conversion is turned on; that is the other kind. |
| USR-TCP232-410S RS485 socket | 26 | [Quick start](https://www.pusr.com/download/M4/USR-TCP232-410S%20Quick%20Start%20Guide.pdf): local port 23 on RS232 and 26 on RS485. The Chinese manual's first socket defaults to 23. Solis uses the RS485 socket, so the preset is 26. Type 23 if the stick is on the RS232 socket. |
| ESPHome stream server | 6638 | Common port of the ESPHome `stream_server` UART bridge. Not a Solis factory default. The bridge is generic. |

## DHCP and a moving address

Solarman sticks still match MAC OUI `BC:54:F9`. Elfin and USR hostnames above are extra DHCP matchers. They do not create an entry. They remember the MAC of a logger already configured at that IP, and the next lease with that MAC updates the host of that same unique id. Give every gateway a DHCP reservation. If the address changes before a MAC has been seen, the repair for a gateway that refuses TCP says to use Reconfigure. That repair and the framing repair are fixable confirmations. An S5 whose port 502 later closes gets its own repair: Solis does not officially support local access, and the firmware update is the usual cause.

## What was implemented from this note

| Kind in the config entry | Devices | Why it is in the tree |
| --- | --- | --- |
| `solarman` | DLS-W, DLS-L, DLS-WL when it answers UDP 48899, LSW-3, LSE-3, other IGEN sticks | Already implemented. V5 on TCP 8899, then Modbus TCP on that port, then port 30003. |
| `s2_wl_st` | Solis S2-WL-ST | Already implemented. Modbus TCP, function 4, function 3 after exception 1. |
| `modbus_tcp` | Waveshare RS485-to-Ethernet in Modbus TCP mode, Elfin EW11 with protocol set to Modbus, USR-TCP232 in RTU-to-TCP conversion, any other gateway that exposes MBAP | Same frames as the S2 path. Different failure meaning: the bridge is mains or PoE powered and is not Solis firmware. |
| `s5_wifi_st` | Solis S5-WiFi-ST | Same frames as the S2. Labelled experimental. Many firmware builds close port 502. Auto-detect cannot tell an S5 from an S2. |
| `rtu_tcp` | Elfin EW11, USR-TCP232, Waveshare and ESP TCP-serial bridges left in transparent mode | Raw Modbus RTU on a TCP socket, no MBAP header. Labelled experimental because the port and the mode are whatever the user configured. |

Not implemented, and why, is in the last section.

## Product line

Solis sells the stick and the inverter as separate pieces. The stick is
powered by the inverter on the 4-pin or USB plug, except a LAN socket
that can also be reached from a laptop at `10.10.100.254` during setup.
A cellular stick has a SIM and no Ethernet. An RS485 gateway is not a
Solis product. It sits on the inverter's RS485 pins and is powered on
its own.

Naming that shows up in manuals and in Home Assistant threads:

| Name | What it is |
| --- | --- |
| S2-WL-ST | Solis Wi-Fi + LAN logger. 4-pin and USB variants. The only logger the [Solis developer Modbus page](https://developer.soliscloud.com/guide/modbus.html) lists for Modbus TCP, from firmware `100101B4`. |
| S3-WIFI-ST | Solis Wi-Fi only. No RJ45. Grouped with S2, S4 and S5 in the [Wi-Fi configuration article](https://solis-service.solisinverters.com/en/support/solutions/articles/44002156150-solis-wi-fi-configuration-for-s2-wl-st-s3-wifi-st-s4-wifi-st-and-s5-wifi-st-dataloggers). |
| S4-WIFI-ST | Newer Solis Wi-Fi stick, including a USB variant used on later inverters. Same setup article. No Modbus chapter. |
| S5-WiFi-ST | Solis Wi-Fi stick. Users had Modbus TCP on port 502 until a firmware update closed it. [solis_modbus issue 432](https://github.com/Pho3niX90/solis_modbus/issues/432). |
| S1-W4G-ST | Older Wi-Fi stick for the 4G inverter generation. 4-pin and USB. Not the cellular logger. [alienatedsec](https://github.com/alienatedsec/solis-ha-modbus-cloud) lists the 4-pin unit as untested and the USB unit as the wrong connector for a 4-pin splitter. |
| DLS-W | Solis-branded IGEN / Solarman Wi-Fi stick. V5 on TCP 8899. |
| DLS-L | Solis-branded IGEN LAN stick. This repository's own stick (serial `1902137671`) speaks V5 on 8899. Other write-ups describe plain Modbus TCP, sometimes on port 3389. Treat the family as "try 8899, override the port if needed", not as one port. |
| DLS-WL | Name used for a Wi-Fi + LAN Solarman stick. No separate protocol document. If UDP 48899 answers, it is the Solarman path. |
| DLS-LAN / LSE-3 / LSW-3 | IGEN model names for the same V5 family. LSW is Wi-Fi, LSE is Ethernet. [pysolarmanv5](https://github.com/jmccrohan/pysolarmanv5) targets them. |
| 4G / GPRS stick | Cellular logger. SIM, no LAN port in the product category. SolisCloud only. |
| S6 USB loggers | USB plug on newer S6 inverters. No published local listener. The 4-pin RS485 splitter does not fit. |

Setup addresses that are **not** the address Home Assistant should store
once the device is on the house LAN:

| Device | Setup UI | House LAN |
| --- | --- | --- |
| S2-WL-ST | `http://10.10.100.254`, user `admin`, password set on first login. Direct cable. [Solis service article](https://solis-service.solisinverters.com/en/support/solutions/articles/44002530087-solis-s2-wl-st-modbus-tcp-communication). | DHCP. IP is on the SolisCloud logger page. |
| S3 / S4 / S5 Wi-Fi | App or the setup network `D_<serial>` / `Solis_<serial>`, password `123456789`. Bluetooth name `D_<serial>` on the sticks that have it. [Wi-Fi article](https://solis-service.solisinverters.com/en/support/solutions/articles/44002156150-solis-wi-fi-configuration-for-s2-wl-st-s3-wifi-st-s4-wifi-st-and-s5-wifi-st-dataloggers). | DHCP, or no local IP at all once the ports are closed. |
| IGEN / DLS / LSW | `http://10.10.100.254` on the Hi-Flying module while associated to its own AP. Hidden pages `/config_hide.html` (DLS-W) and `/hide_set.html` (DLS-L, user `admin`) are described by [alienatedsec](https://github.com/alienatedsec/solis-ha-modbus-cloud). | DHCP. UDP 48899 returns `ip,mac,serial`. |
| Elfin EW11 | `http://10.10.100.254`, user `admin`. The module brings up its own AP. [HA community thread](https://community.home-assistant.io/t/modbus-with-ew11-qdy30a-rs485-water-level-measure-probe/688694), [Akkudoktor wiring note](https://akkudoktor.net/uploads/short-url/1hXnXDmbSALzfeAvuxrK6qHrnxZ.pdf). | DHCP or a static address the user sets. Not discovered as a Solis stick. |
| USR-TCP232-410S | Web UI. Default search uses UDP port 48899 and the keyword `WWW.USR.CN`, which is **not** the IGEN probe. [USR manual](https://www.pusr.com/uploads/20230925/cc816d60345d688cbda902742c6aeafb.pdf). | Static address recommended. Community default local port is 23 until the user changes it. [HA thread](https://community.home-assistant.io/t/solis-inverter-modbus-integration/292553/57). |
| Waveshare RS485 TO ETH | Vendor tool or web UI. Factory address is on the label (installers commonly move it to a static address such as `192.168.1.201`). | Static. No Solis discovery. |

## Protocols

### Solarman V5

TCP 8899. Frame starts `0xA5`, ends `0x15`, logger serial in the header.
A wrong serial is silence. Discovery is UDP 48899, probes
`WIFIKIT-214028-READ` and `HF-A11ASSISTHREAD`, reply `ip,mac,serial`.
The codec in `solarman.py` follows
[pysolarmanv5](https://pysolarmanv5.readthedocs.io/en/latest/solarmanv5_protocol.html).
IGEN does not publish a firmware image. The lab emulator is built from
that layout and from frames captured on the DLS-L in
[solarman-local.md](solarman-local.md).

Some LAN sticks also answer plain Modbus TCP (MBAP, no V5 wrapper) on
8899 or on 30003. pysolarmanv5 issue 5, issue 22, the openHAB and evcc
reports, and `deye-inverter-mqtt` mode `mbtcp` are the sources already
quoted in the Solarman note. The reader tries V5 first and remembers
which framing answered. A reset is not a reason to open the other
framing in the same breath.

DLS-W does **not** speak plain Modbus TCP.
[fboundy's overview](https://github.com/fboundy/ha_solis_overview) and the
[solis_modbus connection table](https://solis-modbus.readthedocs.io/en/latest/)
both say so. V5 is the local path, and it can run beside SolisCloud.
That is the normal IGEN design: the cloud session and a local V5 client
are different sockets.

Quirks, already handled and unchanged:

- About five or six TCP sessions, and a reply can be copied to every
  client (pysolarmanv5 issue 39). This integration keeps one socket.
- A scan of a register the inverter does not have, or too many
  reconnects, resets the local session for minutes while SolisCloud
  still works. Backoff starts at 10 seconds and doubles to about 10
  minutes. Registers at 3050 and above are not retried after one failure.
- The stick is off when the inverter is off. No route at night is
  asleep, not a repair.
- DHCP: MAC OUI `BC:54:F9` in the manifest. The config unique id is the
  numeric logger serial, so a new lease updates the same entry.

Port 3389 appears once, in
[fboundy/ha_solis_overview](https://github.com/fboundy/ha_solis_overview),
as "DLS-L via core Modbus TCP". The same author's
[umodbus test script](https://github.com/fboundy/ha_solis_modbus/blob/main/umodbus_test.py)
puts a DLS-L on port 8899. This repository's captured stick is V5 on
8899. 3389 is therefore a manual port override (Solarman kind, or
Modbus TCP gateway kind), not a new auto-detect step. Adding it to the
automatic scan would add a timeout on every setup for a port one
secondary table mentions.

### Solis Modbus TCP (S2-WL-ST, and S5 when the port is open)

MBAP on TCP 502. Slave 1 unless the inverter was changed. Information
registers are function `04`. The official Modbus Poll demo uses function
`03` at address 3000. This integration sends function 4 and, on
exception 1 only, function 3. There is no V5 header and no UDP 48899
reply.

Vendor statement, quoted on the developer platform: Modbus TCP is
currently the S2-WL-ST from firmware `100101B4`, and "support will be
extended to other data loggers in future firmware releases." That
sentence is why S3 and S4 are not given a local transport here.

Firmware behaviour is the S2 note. Short version:

- Enabling TCP on older firmware disconnects SolisCloud
  ([service article](https://solis-service.solisinverters.com/en/support/solutions/articles/44002530087-solis-s2-wl-st-modbus-tcp-communication)).
- `100121cf`, `100121d0` and `100141dx` are the builds reported to allow
  both. `1001318c` wedges port 502 for minutes after a disconnect.
  `100141d9` left the port open and then timed out generic clients.
  Some later updates close 502 and port 80 together.
- One local connection. A second client, including SolisCloud on
  firmware that has only one slot, is silence rather than a refused port.
- No mDNS, no published MAC OUI. Reserve the DHCP lease.

S5-WiFi-ST used the same port and the same Modbus TCP client in
[Pho3niX90/solis_modbus](https://solis-modbus.readthedocs.io/en/latest/).
[Issue 432](https://github.com/Pho3niX90/solis_modbus/issues/432) (2026)
shows ping succeeding and both port 502 and port 80 closed after a
firmware update, while SolisCloud kept working. Solis support told one
reporter to buy an S2-WL-ST. The product page for the S5 does not
advertise Modbus TCP the way the S2 page does. The integration therefore
exposes S5 as an experimental manual choice that runs the S2 probe. It
does not add S5 to auto-detect as a separate kind: a live port 502 is
indistinguishable from an S2, and auto-detect keeps labelling that
success as `s2_wl_st` so existing unique ids stay `s2:`. Picking S5
stores kind `s5_wifi_st` and the same `s2:` unique id, so the stick is
not added twice.

### Modbus TCP gateway

The inverter speaks Modbus RTU on RS485, 9600 8N1, slave 1
([developer platform](https://developer.soliscloud.com/guide/modbus.html)).
A gateway in "Modbus TCP to RTU" mode accepts MBAP from Home Assistant
and puts RTU on the wire. From this integration's side the socket is
the S2 socket: function 4, then function 3 on exception 1, same register
probe.

Devices with that mode confirmed against a Solis inverter:

| Device | How people run it | Source |
| --- | --- | --- |
| Waveshare RS485 TO ETH / POE / Wi-Fi | TCP server, port 502, transfer protocol Modbus TCP, gateway type "multi-host non-storage" when the Solis stick must stay on the bus, baud 9600 8N1. | [solis_modbus manual](https://solis-modbus.readthedocs.io/en/latest/) connection type `WAVESHARE`. [fboundy umodbus script](https://github.com/fboundy/ha_solis_modbus/blob/main/umodbus_test.py) reads a Waveshare on port 502 with the same calls as an S2. [SDG Electronics](https://sdgelectronics.co.uk/articles/solis-local-modbus-keep-solis-cloud/) and [alienatedsec/solis-ha-modbus-cloud](https://github.com/alienatedsec/solis-ha-modbus-cloud). |
| Elfin EW11 / EE11 | Set the serial protocol to Modbus and the local port to 502. HA's Modbus integration then uses `type: tcp`. | [solis_modbus docs](https://solis-modbus.readthedocs.io/en/latest/) issues 179 and 432. [Akkudoktor PDF](https://akkudoktor.net/uploads/short-url/1hXnXDmbSALzfeAvuxrK6qHrnxZ.pdf). [HA community EW11 thread](https://community.home-assistant.io/t/modbus-with-ew11-qdy30a-rs485-water-level-measure-probe/688694). |
| USR-TCP232-410S and the M4 family | TCP server plus "Modbus RTU <=> Modbus TCP". Local port is often 23 until changed; 502 is the usual choice. | [USR-TCP232-410S manual](https://www.pusr.com/uploads/20230925/cc816d60345d688cbda902742c6aeafb.pdf). [HA thread](https://community.home-assistant.io/t/solis-inverter-modbus-integration/292553/57). |

Pho3niX90's Waveshare option is the same `AsyncModbusTcpClient` as the
S2 option, plus a flag because some of their hybrid sensors read high.
That flag is not a different header. This integration does not apply a
second scale. A value that is off by a factor of 256 is the symptom of
the gateway being in transparent mode, or of reading the wrong register
width, and the form tells the user to switch kind.

Two masters on one RS485 pair collide. Unplugging the Solis stick gives
a clean bus and drops SolisCloud. The pattern that keeps both is the
one alienatedsec and SDG describe: one Waveshare channel on the inverter
in multi-host mode, a second channel that carries the stick's RTU into
that gateway as a Modbus TCP client. This integration only connects to
the channel that faces the inverter. It does not proxy the stick.

Failure meaning is not the S2 firmware story:

- Nothing accepts TCP: the gateway is down, the IP is wrong, or the port
  is wrong. Last good power is held and a repair opens after 10 minutes.
  It is not "night", because a PoE or mains gateway does not die with
  the inverter. A gateway that is powered from the inverter's USB port
  will look the same when the inverter sleeps. That limitation is
  stated in the form text.
- TCP accepts and the read times out or the gateway closes: the inverter
  is not answering. Live power goes to 0, totals stay, source `asleep`.
  That is the night case for a gateway that stays up.
- TCP accepts and the bytes are not MBAP: wrong mode. Pick RTU over TCP,
  or enable Modbus conversion on the gateway. A repair opens after 10
  minutes. Power is held, not zeroed, because this is a configuration
  fault.

There is no discovery. The user types the host. The port defaults to
502 and the slave to empty (probe 1, then 2, then 3). The config unique
id is `tcp:` plus the logger label or the inverter serial, never the IP.

### Modbus RTU over TCP

Transparent mode copies the RS485 bytes onto the TCP stream. There is
no MBAP header, no transaction id, and no unit id separate from the
RTU slave byte. The frame is slave, function, address, count, CRC16,
and the response is slave, function, byte count, data, CRC16. An
exception is five bytes.

This is what an Elfin, a USR device, or a Waveshare does when "protocol
conversion" is left off, and what an ESP TCP-to-UART bridge does. The
port is whatever was configured. The experimental option defaults to
8899 because that is the Hi-Flying transparent default and a common
Elfin setting. A USR unit that was never reconfigured listens on 23.
The help text says to type the port.

The client sends one RTU frame at a time on one socket, waits one
second between blocks, checks the slave byte and the CRC, and retries
function 3 once when function 4 returns exception 1. A mismatched slave
byte, a bad CRC, a short frame, a timeout and a TCP reset are separate
errors. Auto-detect does not send RTU. A V5 stick on 8899 would see a
bare RTU frame as noise, and a scan that probes RTU after V5 would open
a second session into a stick that locks up when it is talked to twice.

### Proprietary or cloud only

S3-WIFI-ST has no local Modbus listener.
[pysolarmanv5](https://github.com/jmccrohan/pysolarmanv5) says it is not
supported (issue 8). The solis_modbus table marks it unsupported
(issues 265 and 347). alienatedsec says it relies on the cloud.
People who want local data add a gateway on RS485, or replace the stick
with an S2-WL-ST.

S4-WIFI-ST has no vendor Modbus statement and no project that names a
port. The developer page still says Modbus TCP will be extended "in
future firmware". That is not enough to ship a client.

S1-W4G-ST has no published frame format. If a unit answers the IGEN
UDP probe it is a Solarman stick and the existing path will read it.
That is a hypothesis, not a supported claim.

4G and GPRS sticks upload over the mobile network. They have no Ethernet
address to probe. Local data means an RS485 gateway beside the stick,
not a session to the stick.

ESPHome's own Modbus controller already publishes sensors. Pointing this
integration at the same UART as well would put two masters on the bus.
An ESP that exposes Modbus TCP, or a transparent TCP serial port, is
the gateway path or the experimental RTU path. This integration does
not open `/dev/tty` and does not speak ESPHome's native API. USB-RS485
adapters are the same decision: [solis_modbus](https://solis-modbus.readthedocs.io/en/latest/)
supports them as a serial connection, including an ESPHome serial
proxy. Doing that here would be a second product.

## Matrix

| Dongle | Protocol | Ports | Discovery | Local access | In this integration | Confidence |
| --- | --- | --- | --- | --- | --- | --- |
| DLS-W, LSW-3 | Solarman V5 | TCP 8899, UDP 48899 | UDP `WIFIKIT-214028-READ`, `HF-A11ASSISTHREAD` | Yes, beside SolisCloud | `solarman` | Verified. pysolarmanv5, this repo's capture, fboundy overview. |
| DLS-L, LSE-3, serials that start like `19…` | V5, and on some firmware plain Modbus TCP | 8899, sometimes 30003 | UDP 48899 | Yes | `solarman`. Port override for 30003 is automatic after V5 fails on 8899. | Verified for 8899 V5 (this repo). Reported for plain MBAP on 8899 and 30003. |
| DLS-L via port 3389 | Modbus TCP, in one overview | 3389 | None documented | Maybe | Manual port on `solarman` or `modbus_tcp` | Reported once, contradicted by the same author's 8899 script. Not auto-detected. |
| DLS-WL | Assumed V5 if it is an IGEN Wi-Fi+LAN stick | 8899 if so | UDP 48899 if so | If discovery answers | `solarman` when the probe matches | Guess that the name is a Solarman SKU. No separate spec. |
| S2-WL-ST | Modbus TCP | 502 | None. Setup AP `D_<serial>` / `Solis_<serial>`. Direct cable `10.10.100.254`. | Yes, firmware-dependent, often one client | `s2_wl_st`. Auto-detect tries 502 first. | Verified. Solis developer page and service article. |
| S5-WiFi-ST | Modbus TCP while the firmware leaves 502 open | 502 | None | Sometimes. Later firmware closes 502 and 80. | Experimental manual kind `s5_wifi_st`. Auto-detect success on 502 stays `s2_wl_st`. | Reported. solis_modbus docs and issue 432. Not a vendor promise. |
| S3-WIFI-ST | Cloud only | None | App / `D_<serial>` | No | Not supported | Verified by absence in the vendor Modbus page plus pysolarmanv5 and solis_modbus. |
| S4-WIFI-ST, S6 USB | Cloud only, as far as anyone has published | None found | App | No published listener | Not supported | Guess from missing documentation, not from a negative vendor sentence. |
| S1-W4G-ST | Unknown. Solarman if UDP 48899 answers. | Unknown | Unknown | Untested in every project read for this note | Not a separate kind | Guess. |
| 4G / GPRS stick | SolisCloud over cellular | None | None | No | Not supported. Use a gateway on RS485. | Verified as a product category (no Ethernet). No counterexample found. |
| Waveshare RS485-to-Ethernet, Modbus TCP mode | Modbus TCP | 502 in the Solis write-ups | None | Yes. Multi-host mode keeps the stick on SolisCloud. | `modbus_tcp` | Verified. solis_modbus, fboundy, SDG, alienatedsec. |
| Elfin EW11 / EE11, Modbus mode | Modbus TCP | User sets 502 | Hi-Flying web UI at `10.10.100.254`. UDP 48899 is plausible and not used. | Yes | `modbus_tcp` | Reported on Solis (solis_modbus 179, 432) and on the EW11 itself (HA, Akkudoktor). |
| Elfin / USR / Waveshare transparent | Modbus RTU over TCP | 8899, 23, or whatever was set | None that this integration should treat as a Solis stick | Yes if baud is 9600 8N1 and only one master polls | Experimental `rtu_tcp` | Verified as a protocol. The Solis baud and the default port are reported, not a single vendor Solis app note. |
| USR-TCP232 Modbus conversion | Modbus TCP | 23 by default, 502 when set | UDP 48899 keyword `WWW.USR.CN`, not the IGEN probe | Yes | `modbus_tcp`, type the port | Verified in the USR manual and one Solis HA thread. |
| ESPHome Modbus controller | ESPHome's own sensors | n/a | n/a | Yes, inside ESPHome | Not this integration | Verified. Two masters would share the bus. |
| ESP TCP-UART bridge | RTU over TCP or MBAP if the firmware converts | User sets it | None | Yes | `rtu_tcp` or `modbus_tcp` | The bridge is generic. No Solis-specific firmware was required to describe the frames. |

## Prior art

| Project | What it supports | What this integration copies |
| --- | --- | --- |
| [jmccrohan/pysolarmanv5](https://github.com/jmccrohan/pysolarmanv5) | V5 codec, sync and async, umodbus. Documents that S3-WIFI-ST is not a V5 stick and that some Ethernet sticks are native Modbus TCP. | Frame layout only. The library is synchronous and owns the socket, so it is not imported. Stated again in [solarman-local.md](solarman-local.md). |
| [davidrapan/ha-solarman](https://github.com/davidrapan/ha-solarman), [StephanJoubert/home_assistant_solarman](https://github.com/StephanJoubert/home_assistant_solarman) | One V5 transport, a YAML profile per inverter brand, UDP 48899 discovery. | Discovery probes and the idea that the inverter profile is data, the stick protocol is code. They do not switch transports. |
| solarman-mqtt and deye-inverter-mqtt | V5 and a `mbtcp` mode for sticks that speak MBAP on 8899. | The order "V5, then MBAP on the same port, then 30003". |
| [Pho3niX90/solis_modbus](https://github.com/Pho3niX90/solis_modbus) | S2, Waveshare, Elfin, USB-RS485, ESPHome serial proxy. S5 marked fragile. S3 and DLS-W marked unsupported for this client because it is MBAP, not V5. | Connection-type list. One MBAP client. Retries kept at one. No second scale for Waveshare. |
| [fboundy/ha_solis_modbus](https://github.com/fboundy/ha_solis_modbus) and [ha_solis_overview](https://github.com/fboundy/ha_solis_overview) | DLS-L and S2 as Modbus TCP, DLS-W as Solarman, S3 as cloud. | The family split. Port 3389 is recorded and not auto-detected. |
| [hultenvp/solis-sensor](https://github.com/hultenvp/solis-sensor) | SolisCloud only. This repository is the rewrite. | Cloud remains the fallback, and control writes stay unimplemented because polling them drops the stick session. |
| [wills106/homeassistant-solax-modbus](https://github.com/wills106/homeassistant-solax-modbus) | Plugin per brand, TCP or serial selected in the form. Solis plugin has been used against S2 firmware `100121b4`. | Manual transport selection when discovery cannot tell devices apart. Not the plugin loader. |
| [alienatedsec/solis-ha-modbus-cloud](https://github.com/alienatedsec/solis-ha-modbus-cloud) and [SDG Electronics](https://sdgelectronics.co.uk/articles/solis-local-modbus-keep-solis-cloud/) | How to keep the stick on SolisCloud while HA polls a Waveshare. | Documented as wiring, not implemented as a second socket. |
| Home Assistant core Modbus | Generic TCP, RTU-over-TCP, and serial. | People who only want a few registers already have this. This integration exists to decode the Solis map, keep entity ids on the inverter serial, and fall back to SolisCloud. |

Forums that repeat the same split, without adding a protocol the table
does not already have: the Home Assistant thread
[Solis inverter Modbus](https://community.home-assistant.io/t/solis-inverter-modbus-integration/292553),
the solis_modbus discussions linked from the S2 note (154, 31, 379),
and the photovoltaikforum / Tweakers threads that point Dutch and
German owners at either Solarman port 8899 or an S2 on port 502. Those
threads were not re-quoted line by line where a primary source already
states the port.

## Several transports in one Home Assistant integration

The practices below are what the quality scale asks for
([config flow](https://developers.home-assistant.io/docs/config_flow_index/),
[integration quality scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/))
and what the projects above converged on after breaking sticks.

**A driver is a small module, the snapshot is shared.** Entity platforms
never see a socket. `SolisLocalTransport` still returns a
`SolisSnapshot`. Adding a dongle adds a `DongleSpec` in
`custom_components/solis/loggers/` and, when the framing is new, a
client with `read_input` / `close`. The S5 spec has no new client. The
RTU spec has `loggers/rtu.py`. The registry is the list the form shows.

**Probe capabilities, do not trust the label.** Auto-detect opens port
502 with a short timeout, then port 8899. It stops on the first framing
that returns a Solis register map. It does not try RTU, port 3389, or
mDNS. mDNS is not documented for any Solis stick read for this note.
Sending extra framings to a locked Solarman stick is how those sticks
get stuck.

**Manual selection when the probe would lie.** A Waveshare on port 502
and an S2 answer the same MBAP probe. Auto-detect calls that an S2,
which is the right unique id for an S2 and the wrong night behaviour
for a gateway. The form says so, and the gateway kind is the correction.
The same is true for transparent RTU: nothing in the auto scan should
guess it.

**Connection identity lives on the config entry. Poll knobs live in
options.** Host, port, kind, slave, serials and the detection code are
data. Scan interval, HTTP timeout, endpoint gap and timezone are
options. Reconfigure is the flow that changes the dongle without
deleting entities. A local-only entry reopens the logger step with the
saved values filled in. A cloud entry can attach a logger from the
reconfigure form. Options do not ask for the IP again.

**Unique id is never the IP.** Solarman: the numeric logger serial.
S2 and S5: `s2:` plus the stick serial or the inverter serial. Gateway:
`tcp:` plus that token. RTU: `rtu:` plus that token. Entity ids stay on
the inverter serial, so cloud and local share a device. A second entry
for the same unique id updates the host instead of creating a duplicate.
DHCP still matches only OUI `BC:54:F9`. A stored MAC on any kind still
updates that entry. No new OUI was added, because a wrong OUI would
claim every device from that vendor.

**One socket, one request, a gap, then backoff.** Register blocks are
one second apart. The poll default is 60 seconds and cannot be faster
than 30. A reset waits at least 10 seconds, then longer, up to about
10 minutes, and each step is one attempt. Gateways get the same pause.
A retry storm is what desynchronises transaction ids on an S2 and what
floods a 9600 baud bus.

**Night and failure are different.** A stick powered by the inverter
goes to `asleep` when there is no route, and live power is 0. A gateway
that refuses the port goes to `degraded` and holds the last power,
because the bridge is the thing that is down. A gateway that accepts
TCP and gets no Modbus answer goes to `asleep`, because the inverter
is the thing that is down. SolisCloud in `both` mode still fills empty
energy counters and still supplies a fresh sample while the local
session is paused. Cloud being down does not drop a good local read.
Local being asleep does not replace live power with a stale cloud
sample.

**Diagnostics say which transport and why.** The report includes the
kind, protocol, port, slave, register map, whether the kind is
experimental, and the detection code (`auto_s2`, `auto_solarman`,
`manual_modbus_tcp`, …) with a fixed sentence. Host, MAC and serials
stay redacted.

**Repairs are for faults a person can act on.** Closed port 502, a
silent S2, a local reset that lasts 10 minutes, a gateway that is
down, and a gateway that is speaking the wrong framing. Night does not
open a repair.

## Sources

- Solis developer platform, Modbus: <https://developer.soliscloud.com/guide/modbus.html>
- Solis service, S2-WL-ST Modbus TCP: <https://solis-service.solisinverters.com/en/support/solutions/articles/44002530087-solis-s2-wl-st-modbus-tcp-communication>
- Solis service, Wi-Fi setup for S2, S3, S4 and S5: <https://solis-service.solisinverters.com/en/support/solutions/articles/44002156150-solis-wi-fi-configuration-for-s2-wl-st-s3-wifi-st-s4-wifi-st-and-s5-wifi-st-dataloggers>
- S2-WL-ST quick installation guide V2.0 (20241028): <https://www.solisinverters.au/uploads/files/202502/Solis_Manual_S2-WL-ST_V2,0(20241028).pdf>
- pysolarmanv5: <https://github.com/jmccrohan/pysolarmanv5> and <https://pysolarmanv5.readthedocs.io/en/latest/solarmanv5_protocol.html>
- davidrapan/ha-solarman: <https://github.com/davidrapan/ha-solarman>
- StephanJoubert/home_assistant_solarman: <https://github.com/StephanJoubert/home_assistant_solarman>
- Pho3niX90/solis_modbus: <https://github.com/Pho3niX90/solis_modbus> and <https://solis-modbus.readthedocs.io/en/latest/>
- S5 firmware closing port 502: <https://github.com/Pho3niX90/solis_modbus/issues/432>
- fboundy/ha_solis_modbus: <https://github.com/fboundy/ha_solis_modbus>
- fboundy/ha_solis_overview: <https://github.com/fboundy/ha_solis_overview>
- hultenvp/solis-sensor: <https://github.com/hultenvp/solis-sensor>
- wills106/homeassistant-solax-modbus: <https://github.com/wills106/homeassistant-solax-modbus>
- alienatedsec/solis-ha-modbus-cloud: <https://github.com/alienatedsec/solis-ha-modbus-cloud>
- SDG Electronics, local Modbus without dropping SolisCloud: <https://sdgelectronics.co.uk/articles/solis-local-modbus-keep-solis-cloud/>
- USR-TCP232-410S manual: <https://www.pusr.com/uploads/20230925/cc816d60345d688cbda902742c6aeafb.pdf>
- Home Assistant, Solis Modbus thread (USR-TCP232-410S): <https://community.home-assistant.io/t/solis-inverter-modbus-integration/292553/57>
- Home Assistant, Elfin EW11: <https://community.home-assistant.io/t/modbus-with-ew11-qdy30a-rs485-water-level-measure-probe/688694>
- Home Assistant config flow: <https://developers.home-assistant.io/docs/config_flow_index/>
- Home Assistant integration quality scale: <https://developers.home-assistant.io/docs/core/integration-quality-scale/>
