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
and form parsing), `solarman.py` (Solarman V5 codec, Modbus TCP client,
UDP discovery, per-host connection gate), `registers.py` (string and hybrid
register maps), and `loggers/` (the logger-kind registry, port-80
fingerprinting, the RTU-over-TCP client, bridge probes).

Device research lives in `docs/research/solarman-local.md`,
`docs/research/s2-wl-st.md` and `docs/research/dongles.md` (input notes, not
spec nodes).

## Logger kinds

A kind is a `DongleSpec` in `loggers/__init__.py`; adding a dongle adds a spec
there and, only for a new framing, a client with `read_input` and `close`. The
config form reads `SELECTABLE_KINDS`, so the list is not copied.

| Kind | Framing | Port | Notes |
| --- | --- | --- | --- |
| `solarman` | V5, then plain Modbus TCP on the same port, then 30003 | 8899 | Found by UDP 48899 discovery and DHCP OUI `BC:54:F9`. |
| `s2_wl_st` | Modbus TCP | 502 | Solis S2-WL-ST from firmware `100101B4`. |
| `modbus_tcp` | Modbus TCP | 502 or typed | Waveshare / Elfin / USR gateway; mains-powered, so a refused port is a fault, not night. |
| `s5_wifi_st` (experimental) | Modbus TCP | 502 | Only while firmware keeps 502 open. |
| `rtu_tcp` (experimental) | Raw Modbus RTU on TCP | Vendor factory port | Transparent bridges; never auto-detected. |

## Decisions

- **In-tree codec.** pysolarmanv5 is synchronous, owns its socket and pulls in
  `umodbus`; the coordinator needs one async socket held open with paced
  reads. So the small V5 subset lives in `solarman.py` and the manifest has
  no requirements.
- **Auto-detect probes 502 then 8899 and stops at the first Solis map.** It
  never tries RTU, other ports or mDNS: extra framings are how Solarman
  sticks lock up.
- **Port 502 is named from port 80, not Modbus.** An S2 and a gateway return
  the same registers, and a second Modbus session (or FC43) can wedge S2
  firmware `1001318c` for minutes. The device's web page decides; an
  unnamed page makes the form ask, with S2 pre-selected.
- **One socket, one request, a gap.** Register blocks are 1 s apart; one
  connection per host (`host_gate`). A reset after the handshake is a lock:
  one attempt, then a pause of at least 10 s doubling to about 10 min.
- **Read discipline.** Function 4 first, function 3 only after exception 1.
  Slaves 1, 2, 3 are tried unless the form fixes one. 33022 decides string vs
  hybrid; once the 3000 map is known the 33000 range is not read. Registers
  at 3050 and above are tried once and not retried after failing.
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
  token. A new address or MAC reported by the transport is written back to
  the entry via callbacks (`on_host`, `on_protocol`, `on_serial`).

## Boundary

- Home Assistant–free; depends on the shared kernel only. Raises
  `SolisLoggerOffline` / `SolisLoggerBusy` and leaves repairs and poll
  backoff to `coordinator`.
- Read-only: issues Modbus functions 3 and 4 only.
- `loggers/probe.py` currently reaches private helpers in
  `local_transport` and `solarman` (structural debt, see architecture).
