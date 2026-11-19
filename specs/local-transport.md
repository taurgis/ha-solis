---
id: local-transport
type: module-design
status: active
title: Local transport (data loggers on the LAN)
parent: solis-architecture
references: [solis-api-research]
---

## Responsibility

Read the inverter through a data logger on the LAN and return the same
`SolisSnapshot` as the cloud. Files: `local_transport.py`
(`SolisLocalTransport`, the local-first `SolisPriorityTransport`, probing
and form parsing), `web_transport.py` (`SolisWebTransport` and its probe, for
sticks that only serve a web page), `cloud_fill.py` (merging a cloud sample
into a web-page snapshot), `solarman.py` (Solarman V5 codec, Modbus TCP
client, UDP discovery, per-host connection gate), `registers.py` (string and
hybrid register maps), and `loggers/` (the logger-kind registry, port-80
fingerprinting, the RTU-over-TCP client, bridge probes, the web-page HTTP
client and parsers in `loggers/web.py`).

Device research lives in `docs/research/solarman-local.md`,
`docs/research/s2-wl-st.md`, `docs/research/web-page-stick.md` and
`docs/research/dongles.md` (input notes, not spec nodes).

## Logger kinds

A kind is a `DongleSpec` in `loggers/__init__.py`; adding a dongle adds a spec
there and, only for a new framing, a client with `read_input` and `close`. The
config form reads `SELECTABLE_KINDS`, so the list is not copied.

| Kind | Framing | Port | Notes |
| --- | --- | --- | --- |
| `solarman` | V5, then plain Modbus TCP on the same port, then 30003 | 8899 | Found by UDP 48899 discovery (a reply counts only when it names the address it came from, and that is private or loopback) and DHCP OUIs `E8:FD:F8` and `BC:54:F9`. A frame read has an overall deadline of twice the timeout, so a peer that streams junk cannot hold the entry. |
| `s2_wl_st` | Modbus TCP | 502 | Solis S2-WL-ST from firmware `100101B4`. |
| `modbus_tcp` | Modbus TCP | 502 or typed | Waveshare / Elfin / USR gateway; mains-powered, so a refused port is a fault, not night. |
| `s5_wifi_st` (experimental) | Modbus TCP | 502 | Only while firmware keeps 502 open. |
| `rtu_tcp` (experimental) | Raw Modbus RTU on TCP | Vendor factory port | Transparent bridges; never auto-detected. |
| `solis_web` | HTTP GET of `inverter.cgi`, else `status.html`; basic auth `admin` | 80 | S3-class sticks with no Modbus or Solarman port, and IGEN-made Solis sticks that serve `status.html`. Auto-detected last, see below. |

## Decisions

- **In-tree codec.** pysolarmanv5 is synchronous, owns its socket and pulls in
  `umodbus`; the coordinator needs one async socket held open with paced
  reads. So the small V5 subset lives in `solarman.py` and the manifest has
  no requirements.
- **Auto-detect probes 502 then 8899 and stops at the first Solis map.** It
  never tries RTU, other ports or mDNS: extra framings are how Solarman
  sticks lock up. Only when both fail without a connected-but-silent 502 does
  it look at port 80: any Basic-auth `401` plus a readable `inverter.cgi` or
  `status.html` offers `solis_web`. Realms differ between sticks, so parsing
  the data page is what keeps a router or another device from being
  claimed; only the `solis` realm is trusted to report a wrong password
  during detection.
- **Web page stick.** The stick polls the inverter about every five minutes
  and serves the cached values (measured: two changes 5 min 02 s apart over
  12 minutes), the same delay as SolisCloud, so the page is local but not
  faster. It is read at the normal local poll (60 s) so a new sample shows
  within a minute. Password is the web password, blank (or the factory
  `123456789`) meaning "try `123456789`, then the IGEN factory `admin`, and
  keep the one that answers", stored in entry data and redacted. Identity is the inverter
  serial from the page, unique id `web:<serial>`. `inverter.cgi` is tried
  first; a 404 or another format falls back to `status.html`, and the
  layout that answered is kept. A 401 (`web_auth`) holds the last values; after
  three 401s in a row or 10 minutes the coordinator turns it into
  reauthentication, not a repair (at once when there is nothing to hold). A page that is
  not a Solis format, a bad status, or a TCP session that gives no page
  (`web_page`) is a paused session with the usual backoff, not night, and
  opens the `logger_web` repair after 10 minutes; no route is asleep. The
  HTTP client is hand-written on asyncio streams (one request per
  connection, no redirects, size and time caps) because the stick firmware
  pads replies and handles one socket at a time. Evidence and field layout:
  `docs/research/web-page-stick.md`.
- **One paced cloud read.** `SolisPriorityTransport._optional_cloud` is the only
  cloud read behind a local source: at most every 5 minutes (60 s after a
  failure), within a 20 s budget, cached. The fill, the identity read and the
  degraded, asleep and offline fallbacks all use it, so a paused stick never
  turns every poll into a full cloud read. A rejected key or a wrong clock is
  kept in `cloud_error` for the coordinator instead of being swallowed.
- **Cloud fills a web-page stick.** The page lacks month and year energy,
  frequency, phases, strings, the series name, the plant and alarms. In `both`
  mode the page wins for the fields it has and `fill_from_cloud` takes the
  rest from a cached cloud sample, read at most every 5 minutes (60 s after a
  failure) because both sources share the five-minute cycle. A failed read
  keeps the cache; a sample fetched or measured more than 15 minutes ago
  supplies totals and names only, never live readings, phases, strings or
  alarms. A snapshot without a matching fill is `partial`, so the sensor
  platform does not prune the plant's devices. The cloud inverter is matched by serial. Other
  local kinds keep the older rule: cloud identity once, energy fill only when
  the local sample is asleep or degraded (`LocalSource.cloud_fill`).
- **Port 502 is named from port 80, not Modbus.** An S2 and a gateway return
  the same registers, and a second Modbus session (or FC43) can wedge S2
  firmware `1001318c` for minutes. The device's web page decides; an
  unnamed page makes the form ask, with S2 pre-selected.
- **One socket, one request, a gap.** Register blocks are 1 s apart; one
  connection per host (`host_gate`). A reset after the handshake is a lock:
  one attempt, then a pause of at least 10 s doubling to about 10 min.
- **Read discipline.** Function 4 first, function 3 only after exception 1.
  Slaves 1, 2, 3 are tried unless the form fixes one. 3004 answering selects
  the string map, except that on Modbus TCP and RTU links a hybrid type in
  35000 (read once) wins; a V5 stick is never asked 35000 after 3004. When
  3004 is illegal, 35000 and then 33022 decide. Once the 3000 map is known
  the 33000 range is not read. Registers at 3050 and above are tried once
  and not retried after failing.
- **Night is not failure.** No route or a timeout before TCP opens on an
  inverter-powered stick is `asleep`: live power 0, totals kept, no repair.
  A gateway refusing TCP is `degraded` and holds the last power; a gateway
  that accepts TCP but gets no Modbus answer is the inverter off, so
  `asleep`.
- **Both mode.** `SolisPriorityTransport` reads local first. A local lock
  with a fresh cloud sample yields a degraded cloud snapshot; local energy
  counters missing a value are filled from cloud; cloud failure never drops
  a good local read; a stale cloud sample never replaces an asleep local one.
- **Identity.** Config unique ids: Solarman uses the numeric logger serial;
  S2/S5 `s2:` + stick or inverter serial; gateway `tcp:` + token; RTU `rtu:` +
  token; web page `web:` + inverter serial. A new address or MAC reported by the transport is written back to
  the entry via callbacks (`on_host`, `on_protocol`, `on_serial`).

## Boundary

- Home Assistant–free; depends on the shared kernel only. Raises
  `SolisLoggerOffline` / `SolisLoggerBusy` and leaves repairs and poll
  backoff to `coordinator`.
- Read-only: issues Modbus functions 3 and 4 only, and HTTP GET of the two
  status pages.
- `loggers/probe.py` currently reaches private helpers in
  `local_transport` and `solarman` (structural debt, see architecture).
