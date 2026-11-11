# Solis S2-WL-ST local reads

The S2-WL-ST is Solis's own 4-pin Wi-Fi + LAN data logger. It is the stick
the owner is buying to replace a DLS-L that locks up on local requests. It
is not an IGEN / Solarman stick. It does not speak Solarman V5 and it does
not answer the UDP 48899 discovery probes. On firmware that implements it,
the local protocol is ordinary Modbus TCP on port 502.

The inverter behind this note is the same Solis-mini-2500-4G (model E4,
slave 1, serial `110E40182280017`, firmware `0F000F`) described in
[solarman-local.md](solarman-local.md). The register map does not change
with the stick. The transport does.

## What the frames are

| Source | What it says |
| --- | --- |
| [Solis developer platform, Modbus](https://developer.soliscloud.com/guide/modbus.html) | Modbus TCP is available on the S2-WL-ST from firmware `100101B4`. Information registers are function `04H`. Holding registers are function `03H`. RTU on the inverter side is 9600 8N1. |
| [Solis service, S2-WL-ST Modbus TCP](https://solis-service.solisinverters.com/en/support/solutions/articles/44002530087-solis-s2-wl-st-modbus-tcp-communication) (modified 9 Jan 2025) and the [North America copy](https://usservice.solisinverters.com/support/solutions/articles/73000670200-modbus-tcp-communication-guide-for-s2-wl-st-wi-fi-lan-logger) | Port 502, slave 1, Modbus Poll demo uses function 03 at address 3000, length 50, scan 1000 ms, 10 ms between polls, 1000 ms timeout. Direct-cable address `10.10.100.254`. Web UI username `admin`; the first login sets the password. **Enabling TCP/IP disconnects the logger from SolisCloud.** |
| [Pho3niX90/solis_modbus](https://github.com/Pho3niX90/solis_modbus) `modbus_controller.py` and `client_manager.py`, and the [manual](https://solis-modbus.readthedocs.io/en/latest/) | The S2 path is pymodbus `AsyncModbusTcpClient` on port 502 with `retries=1` and `read_input_registers` (function 4). `CONNECTION_METHOD` is the string `S2_WL_ST` or `WAVESHARE`. Waveshare adds a feature flag; it is not a different header. There is no extra handshake in that code. Discussion [379](https://github.com/Pho3niX90/solis_modbus/discussions/379) calls the S2 connection type a handshake because mbpoll, pymodbus and tcpsolis2mqtt timed out on firmware `100141d9` while this integration's S2 mode answered. The practical differences those clients miss are one socket, function 4 (then 3), no retry storm, and not scanning registers the inverter does not have. |
| [fboundy/ha_solis_modbus](https://github.com/fboundy/ha_solis_modbus) | Same family split already used for the Solarman work: DLS-LAN on 8899, S2-WL-ST on 502, and a warning that a firmware update can close the local port. |
| Quick installation guide S2-WL-ST V2.0 (20241028), [PDF](https://www.solisinverters.au/uploads/files/202502/Solis_Manual_S2-WL-ST_V2,0(20241028).pdf) | 4-pin and USB variants. Default inverter slave address 01. Setup Wi-Fi name `D_<logger serial>`, password `123456789`. The manual does not mention Modbus, a MAC OUI, mDNS, or a hostname. |

This integration therefore sends Modbus TCP function 4 (input registers) and,
only after exception 1 (illegal function), function 3. Slave 1 is tried, then
2, then 3, unless the form fixes the slave. The 3000-range string map is the
one checked on the mini. Register 33022 is read only when 3004 itself returns
exception 2. Registers at 3050 and above, including the eight-byte serial at
3060-3063, are tried once.

The inverter serial SolisCloud shows for this plant is 15 characters
(`110E40182280017`). Registers 3060-3063 are eight bytes, so they cannot hold
that serial. The config-entry unique id is `s2:` plus the logger serial from
the label when the user types it, otherwise `s2:` plus whatever serial the
registers or the typed inverter serial supplied. Entity ids stay on the
inverter serial, shared with a cloud entry in `both` mode.

## Firmware

| Report | Effect |
| --- | --- |
| Developer platform | Port 502 exists from `100101B4`. |
| Official SOP, 9 Jan 2025 | Enabling TCP disconnects SolisCloud. |
| [solis_modbus discussion 154](https://github.com/Pho3niX90/solis_modbus/discussions/154) | `100121cf` ran Modbus and SolisCloud together for two days. On `100141d2` only one device could query port 502 even with max connections set to 10. |
| [solis_modbus manual](https://solis-modbus.readthedocs.io/en/latest/) | `100121cf`, `100121d0` and the `100141dx` releases are the examples that allow cloud and Modbus at once. Some newer firmware closes port 502. Port 80 (the stick web UI) is often dead at the same time. No integration setting reopens a closed port. |
| [wills106/homeassistant-solax-modbus discussion 1427](https://github.com/wills106/homeassistant-solax-modbus/discussions/1427) | `100121b4` kept SolisCloud while solax-modbus polled. |
| [ahinko/tcpsolis2mqtt](https://github.com/ahinko/tcpsolis2mqtt) | On `1001318c`, closing the TCP connection wedges port 502 for about 3 to 6 minutes. Keeping one socket open avoids it. The stick accepts one connection at a time, so holding it also blocks SolisCloud on firmware that has only one slot. |
| [Village Energy](https://villageenergy.zohodesk.com/portal/en/kb/articles/how-do-i-enable-modbus-on-my-solis-inverter) | Firmware older than `100141d2` or `100121cf` causes SolisCloud trouble. They also suggest the inverter's 24-hour switch so the COM board stays up after sunset. This integration does not assume that switch is on. |
| [Static IP article](https://solis-service.solisinverters.com/en/support/solutions/articles/44002777606-setting-a-static-ip-address-for-an-s2-wl-st-wi-fi-lan-logger) | LAN port settings are missing on some firmware (the example is `100171b6`). The default on a LAN is DHCP. |
| [solis_modbus discussion 31](https://github.com/Pho3niX90/solis_modbus/discussions/31) and [discussion 379](https://github.com/Pho3niX90/solis_modbus/discussions/379) | Older sticks take whichever of SolisCloud or the local client connects first. `100141d9` left port 502 open and then timed out every generic client. Max connections was already 2. |

The integration tells these apart:

- No route or a timeout before TCP opens: the stick is off with the inverter.
  Live power is 0, totals stay, the data source reads `asleep`, and no repair
  is opened.
- TCP reset, or a second client while one is already open: the same backoff
  as a Solarman lock. At least 10 seconds, doubling to about 10 minutes. One
  socket is kept and closed on failure. Firmware `1001318c` is why a failure
  closes and then waits, instead of reconnecting in a loop.
- Port 502 refused while the host is up: Modbus TCP is disabled. In `both`
  mode a fresh cloud sample is published on the same inverter serial and the
  data source reads `degraded`. A repair opens only after this has lasted 10
  minutes. It names firmware `100101B4`, the official disconnect note, and
  the `100121cf` / `100141dx` builds that are reported to allow both.
- TCP connects and no Modbus reply arrives: the silent case (one connection
  already taken, or the `100141d9` report). Same degraded path, with a repair
  that says to drop the other client or ask Solis for a both-at-once firmware.

A refused port 502 during auto-detect is not that repair by itself. A DLS-L
refuses 502 because it is not an S2. Auto-detect then tries 8899. The
firmware repair is used when the entry is an S2, or when 502 accepts TCP and
stays silent.

## How the stick is found

| Method | Result |
| --- | --- |
| UDP 48899 `WIFIKIT-214028-READ` / `HF-A11ASSISTHREAD` | Not this stick. Those probes find the DLS-L. |
| mDNS / hostname | Not documented in the official manual, the service articles, or the projects above. The integration does not browse mDNS. |
| MAC OUI | Not published. DHCP in the manifest still matches only `BC:54:F9` (IGEN). If a MAC has been stored on the entry, a DHCP discovery for that MAC updates the IP. Home Assistant will not deliver the event until an OUI is known. |
| Wi-Fi setup network | `Solis_<logger serial>` or `D_<logger serial>`, password `123456789`. Bluetooth name `D_<logger serial>`. Examples in the Solis articles look like `5A12…` and `7A12…`. That serial is not the inverter serial. [Wi-Fi configuration](https://solis-service.solisinverters.com/en/support/solutions/articles/44002156150-solis-wi-fi-configuration-for-s2-wl-st-s3-wifi-st-s4-wifi-st-and-s5-wifi-st-dataloggers). |
| Direct cable or AP web UI | `http://10.10.100.254`, user `admin`. This is the stick's own setup address, not its address on the house LAN. |
| House LAN | DHCP. The IP is on the SolisCloud logger page. Reserve it in the router. |

## Rate and connections

The official demo polls every 1000 ms with 10 ms between frames. solis_modbus
set pymodbus `retries=1` because a retry storm floods the S2-WL, desynchronises
transaction ids, and starves the cloud uplink (`client_manager.py`). Their
manual also cites a Solis gap of more than 300 ms between frames.

This integration keeps one TCP session, waits one second between register
blocks, polls every 60 seconds, and refuses a poll faster than 30. That is
the same floor as the Solarman path. It does not open a second connection to
"try the other framing" after a reset.

## Emulator

`tools/mock_soliscloud/stick.py --kind s2` listens for Modbus TCP, answers
function 3 and function 4, and does not open UDP 48899. `POST /__control`
can turn the lights off (`night`), refuse the port (`port_closed`), accept
TCP and stay silent (`silent`), allow one client (`max_clients`), or close
the port after the client disconnects (`close_disables_port`). Solis does
not publish an S2-WL-ST firmware image; the emulator follows the Modbus TCP
layout in the articles above, not a captured S2 frame. The register values
are the string-inverter capture from the DLS-L on this inverter.
