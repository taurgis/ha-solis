# Solarman local reads

The logger on the bench is an IGEN / Solarman Wi-Fi stick. It is powered by
the inverter. While the inverter is producing, the stick answers at TCP 8899
(Solarman V5) and UDP 48899. The stick that was read here is serial
`1902137671`, MAC `bc:54:f9:f5:7b:5c`. The address that morning was
`192.168.0.105`. That address is a DHCP lease. The integration identifies the
stick by serial and MAC.

## Who already solved the pieces

| Source | What this integration takes from it |
| --- | --- |
| [jmccrohan/pysolarmanv5](https://github.com/jmccrohan/pysolarmanv5) | V5 header layout: start `0xA5`, little-endian length, control, sequence, logger serial, then a prefix, the Modbus RTU frame, a checksum of the bytes after the start, and end `0x15`. Request control is `0x4510`. Response control is `0x1510`. The response Modbus RTU starts at byte 25. |
| [davidrapan/ha-solarman](https://github.com/davidrapan/ha-solarman) and [StephanJoubert/home_assistant_solarman](https://github.com/StephanJoubert/home_assistant_solarman) | Discovery probes `WIFIKIT-214028-READ` and `HF-A11ASSISTHREAD` to UDP 48899. The reply is `ip,mac,serial`. Their Solis string profiles use the same 3000-range function-4 map. |
| Ginlong ModbusRTU protocol, Ver.3.7 (the table mirrored by [voltrus.id](https://www.voltrus.id/ginlong-solis-modbus-rtu-protocol/)) | String-inverter input registers. High word first. |
| [Pho3niX90/solis_modbus](https://github.com/Pho3niX90/solis_modbus) and the Modbus Connect Solis map | Hybrid input registers from 33000. Register 33135 is battery direction: 0 charge, 1 discharge. |
| [fboundy/ha_solis_modbus](https://github.com/fboundy/ha_solis_modbus) | Same stick families (DLS-LAN on 8899, S2-WL-ST on 502) and the warning that a firmware update can close the local port. |
| [wills106/homeassistant-solax-modbus](https://github.com/wills106/homeassistant-solax-modbus) Solis plugin | Battery power is positive while charging. The cloud parser already follows that. |
| [hultenvp/solis-sensor](https://github.com/hultenvp/solis-sensor) | Cloud remains the transport for sticks with no local port. Local discussions there describe the same 8899 stick and the same refusal to poll control registers, because that drops the cloud session. |

pysolarmanv5 3.0.6 is the maintained codec and it still targets Python 3.8.
It is synchronous, it depends on `umodbus`, and it owns the socket. The
coordinator needs one async socket that stays open and waits between reads,
so the codec in `custom_components/solis/solarman.py` is the small V5 subset
this integration needs. The manifest requirements stay empty.

IGEN does not publish a runnable LSW-3 or LSE-3 firmware image, and the
public Solarman projects ship the protocol rather than a firmware blob. The
emulator in `tools/mock_soliscloud/stick.py` is built from the V5 layout and
from frames captured on this stick. `POST /__control` can close both ports
(night) and bring the stick back on another address.

## Frame captured from the stick

A read of hybrid register 33022 came back as:

```text
a513001015b117475160710201f3653c080704000063bab762018402c2c15315
```

The logger serial is bytes 7-10, little endian: `47 51 60 71` =
`1902137671`. The Modbus RTU starts at byte 25 and is function `0x84` with
exception code 2 (illegal data address). A wrong serial gets silence. The
emulator does the same.

## String map, checked against that stick

Function 4, slave 1, just after sunrise, with the inverter waking:

| Address | Raw | Meaning in Ver.3.7 | Value |
| --- | --- | --- | --- |
| 3004-3005 | 0, 0 | Active power, int32 W | 0 W |
| 3008-3009 | 0, 21970 | Total energy, uint32 kWh | 21970 kWh |
| 3010-3011 | 0, 30 | This month, kWh | 30 kWh |
| 3012-3013 | 0, 169 | Last month, kWh | 169 kWh |
| 3014 | 0 | Today, 0.1 kWh | 0.0 kWh |
| 3015 | 59 | Yesterday, 0.1 kWh | 5.9 kWh |
| 3016-3017 | 0, 2690 | This year, kWh | 2690 kWh |
| 3018-3019 | 0, 2717 | Last year, kWh | 2717 kWh |
| 3021 | 2784 | PV1 voltage, 0.1 V | 278.4 V |
| 3022 | 13 | PV1 current, 0.1 A | 1.3 A |
| 3033 | 0 | Phase A voltage, 0.1 V | 0 |
| 3035 | 2349 | Phase C voltage, 0.1 V | 234.9 V |
| 3041 | 272 | Temperature, 0.1 °C | 27.2 °C |
| 3042 | 4999 | Frequency, 0.01 Hz | 49.99 Hz |
| 3043 | 3 | Operating status | see below |

Register 3002 was 0, so the inverter is single phase. Phase A voltage was 0
while 3035 held the grid voltage, so the single published phase uses the
first non-zero of 3033, 3034 and 3035.

Status 1 maps to cloud state 1 (online). Status 2 maps to cloud state 3
(alarm). Status 3 was this waking inverter, which still had grid voltage and
PV voltage, so it stays unknown. It is not published as an alarm.

Pho3niX90's string profile pairs 3021 and 3022 as one uint32 of DC power.
This capture separates them: 3021 is volts and 3022 is amps.

Registers 3050-3070, including the ASCII serial at 3060-3063, did not answer
on this stick before the logger reset the TCP session. The emulator does
answer 3060-3063. A real stick that stays silent there keeps the inverter
serial learned at setup, or `L` plus the logger serial when setup could not
read one. That serial is the entity unique id. It can differ from the serial
SolisCloud uses for the same inverter. Add the cloud account on the same
entry only for energy fields the stick left empty. It does not rename the
local device.

The hybrid auto-detect probes 33022. Exception 2 selects the string map. A
normal reply selects the 33000 hybrid map. Slave 1 is tried, then 2, then 3.

## Night, and a new address in the morning

The stick's LAN light goes out with the inverter. TCP 8899 and UDP 48899 both
stop. Connect attempts time out or get "host unreachable". The emulator
models that by closing both sockets.

The coordinator treats that as the inverter asleep:

- Live power is 0, because the snapshot uses offline state 2 and the existing
  power rule publishes 0 for a fresh offline sample.
- Energy totals stay.
- The data-source sensor reads `asleep`.
- No repair is opened, and the offline message is one info log.
- The poll interval backs off (at least 60 seconds, doubling, capped at 15
  minutes) so the dark stick is not probed at the daytime rate.
- Cloud is not asked to replace the live readings just because the stick is
  dark. In "local and cloud" mode the cloud may fill energy counters that
  the local snapshot left empty. A local total that is already present stays.

When a connect fails during the day, or the first connect after the stick
wakes, the integration broadcasts the two discovery probes, matches the
logger serial, and writes the new IP into the existing config entry. The log
line is one info record: `Solarman logger <serial> is now at <ip>`. No second
entry is created.

DHCP discovery does the same for MAC OUI `BC:54:F9`. The config-entry unique
id is the logger serial, and the flow uses Home Assistant's
`_abort_if_unique_id_configured(updates={"logger_host": ...})` so a lease
change updates that entry.

Setup copy tells the user to run discovery while the inverter is producing.
An empty discovery result is explained there: the stick is off with the
inverter.

## This 4G mini, named from SolisCloud

The inverter on the bench is a Solis-mini-2500-4G, model code E4, Modbus
slave 1, rated 2.5 kW. SolisCloud shows inverter version `0F000F`. Registers
3000 and 3001 on the wire were 15 and 15, which is that version:
`0F` + `000F`. The series and the model code are not in those two registers.
They come from SolisCloud (`machine` / `series`, and `model` when it differs
from the series). Device info is `Solis-mini-2500-4G (E4)` once both are
known. A local-only read with no cloud series is labelled `Solis 4G` and
still uses the 3000-range map.

The cloud total was 21.97 MWh, which is the same 21970 kWh the stick
reported. Power was 0.059 kW while the inverter display showed about 60 W.
That is production. Asleep is only the stick disappearing from the LAN.

The inverter serial SolisCloud uses is `110E40182280017`. Registers
3060-3063 are only eight bytes, and on this stick a read at 3050 or above
resets the session, so the serial is taken from the cloud in `both` mode or
typed in. Entity ids use that serial either way. The placeholder `L` plus
the logger serial is replaced when the real one is learned, and existing
entity rows are retargeted.

## Modbus TCP as well as V5

DLS-L LAN sticks whose serial starts with 19 often answer plain Modbus TCP
(MBAP, no V5 wrapper) on port 8899, slave 1, about 50 registers per request.
Some firmware uses port 30003 instead.

- pysolarmanv5 issue 5
- openHAB pull 17563
- evcc issue 9598
- kbialek/deye-inverter-mqtt mode `mbtcp`
- pysolarmanv5 issue 22 for port 30003

The reader tries V5 first. If the socket opens and the bytes are not V5, it
tries Modbus TCP on the same port and then on 30003, and stores the framing
that answered. A reset is not treated as "try the other framing": that would
open more sessions into a locked stick.

Loggers allow about five or six TCP sessions and can copy a reply to every
client (pysolarmanv5 issue 39, go-solarman-proxy). This integration keeps one
socket, closes it on unload and on any failed read, and does not reconnect
in a loop.

## A reset is not night

After many reconnects, or a scan of a register the inverter does not have,
the stick still accepts TCP and UDP discovery, and it still uploads to
SolisCloud, but every local request is reset. The same behaviour is described
in pysolarmanv5 issue 22, StephanJoubert/home_assistant_solarman issue 523
and davidrapan/ha-solarman issue 586. Recovery is the logger's TCP idle time,
about 300 seconds and sometimes 600, or a power cycle of the inverter.

The 3000-range map is cached. Register 33022 is read only when register 3004
itself returns Modbus exception 2. Registers at 3050 and above are tried at
most once, during setup, and a failure is remembered.

The first pause after a reset is at least 10 seconds. It doubles up to about
10 minutes. Each step is one attempt. In `both` mode a fresh cloud sample is
published on the same inverter serial, the data source reads `degraded`, and
live power is the cloud value, including a few tens of watts. A repair opens
only after the reset has lasted 10 minutes. It does not mention the stick
LED.

A steady red LED on a DLS-L is the normal Solis indication: D1 red means the
inverter link is up, D2 red means the server link is up. It is not a fault
and the repair text does not say that it is.

Night is still no route or a timeout. Live power is 0, totals stay, the data
source reads `asleep`, and the cloud is not asked to replace those live
readings.

Give the stick a DHCP reservation. Do not set a static address on the stick.
The integration follows the serial and the MAC when the lease changes.

Poll defaults follow davidrapan/ha-solarman discussion 208 and
deye-inverter-mqtt: 60 seconds, never faster than 30. Consecutive register
reads are one second apart, inside the 150 ms to 1 s range those projects use.

## Dongles

`SolisLocalTransport` speaks Solarman V5 and, when that is not the framing,
Modbus TCP. A Wi-Fi S3 stick has no local port. Both framings fill the same
`SolisSnapshot`.
