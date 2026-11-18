---
id: solis-goal
type: goal-and-requirements
status: active
title: Solis for Home Assistant — goal and requirements
---

## Goal

ha-solis is a read-only Home Assistant integration (domain `solis`) that brings
a Solis installation into Home Assistant: plants, inverters, batteries, alarms,
data loggers and export power managers (EPMs). It reads the SolisCloud Platform
API v2, a data logger on the LAN, or both. It is a drop-in replacement for
[hultenvp/solis-sensor](https://github.com/hultenvp/solis-sensor) that keeps
config entries and entity history, and it is held to Home Assistant's
**platinum** integration quality scale.

## Problem

The upstream integration needed a portal username, polled the control API
(which drops the logger's cloud session), and keyed entity ids on the entry's
display name, so a rename orphaned history. SolisCloud itself only publishes a
sample about every five minutes, rate-limits each endpoint, and returns
inconsistent units and nulls. Many owners also have a logger that can be read
locally, but each stick family speaks a different framing and fails in its own
way.

## Audience

Home Assistant users with a Solis inverter: grid or hybrid, single or three
phase, with or without a battery — including users migrating from
hultenvp/solis-sensor. Installed through HACS or by copying
`custom_components/solis`; the minimum Home Assistant version is the one in
`hacs.json` (2025.10.0).

## Capabilities

- **Cloud reads.** Authenticate with the SolisCloud API key id and secret only
  (HMAC-signed requests), let the user pick any number of plants under one key,
  and stay under the platform's per-endpoint rate limit.
- **Local reads.** Read the inverter through a logger on the LAN: Solarman /
  IGEN V5 sticks, the Solis S2-WL-ST, Modbus TCP gateways (Waveshare, Elfin,
  USR), and, experimentally, an S5-WiFi-ST with port 502 open and transparent
  Modbus RTU over TCP. A stick with no Modbus port but a status web page is
  read from that page. Auto-detect covers the kinds that can be told apart
  safely; the rest are chosen by hand.
- **Cloud, local, or both.** In `both` mode the local logger is preferred and
  the cloud fills energy totals and covers a paused local session. A web-page
  stick reports few fields, so the cloud also fills month and year energy,
  frequency, phases and strings.
- **Devices and entities.** Plant, inverter, logger and EPM devices with power,
  energy, phase, PV-string, battery, alarm and diagnostic entities
  (catalogue: `docs/entities.md`).
- **Energy-dashboard-safe totals.** Energy counters never publish a dip, a
  null, a zero or an absurd jump inside a period, including across restarts.
- **Honest availability.** A fresh offline sample reads `0`; a stale sample
  or failed poll reads `unavailable`; a logger powered down at night reads as
  `asleep` and keeps totals.
- **Resilience and repairs.** Retries, backoff and circuit-breaking for cloud
  outages; reauthentication for a rejected API key or stick password;
  non-fixable repairs, with a troubleshooting link where one helps, only for
  faults a person can act on outside Home Assistant (clock, empty plant,
  closed port, down gateway, wrong framing); none for night, an outage or
  abnormal cloud data.
- **Migration from hultenvp/solis-sensor.** Config entries and legacy entity
  and device ids are rewritten on startup so history carries over.
- **Discovery and moving addresses.** Solarman / IGEN sticks are matched over
  DHCP (IGEN OUIs) and UDP; a new one is offered as a discovered entry with a
  confirm step, and a known logger is followed to its new address. A logger
  is identified by serial/MAC, never by IP.
- **Diagnostics** with secrets, location, host, MAC, serials, plant ids and
  names redacted.
- **Translations**: English and Dutch.

## Non-goals

- **Inverter control writes.** No `/v2/api/control` or `/v2/api/atRead`, and
  no local Modbus writes. Polling the control API drops the logger's cloud
  session, and a wrong register map can change charge behaviour.
- **The legacy Ginlong `/cpro/` portal API.**
- **Running beside hultenvp/solis-sensor.** The domain stays `solis` so history
  carries over; side-by-side evaluation needs a second Home Assistant.
- **Config subentries per plant**, **integration-derived energy sensors**, and
  **`device_class: energy_storage`**. Subentries would put existing entity ids
  at risk; derived sensors and stored-energy figures would invent data the
  inverter never reported.

## Known limitations

- A web-page stick shows one sample about every five minutes, the same cycle
  as SolisCloud, and only power, temperature, energy today and total,
  firmware and an alarm word. It is the one local kind verified on a real
  stick beyond the Solarman string map (`docs/research/web-page-stick.md`).
- Most logger kinds were never on a bench. S5-WiFi-ST, Modbus gateways, RTU
  bridges and LSW-3 / LSE-3 sticks are built from vendor docs, protocol
  implementations and user reports, and verified only against the emulators
  in `tools/` (see `dev-tooling`).
- Only the 3000-range string register map was checked on a real stick. The
  33000-range hybrid map comes from third-party tables and is unverified on
  hardware.
- A local-only entry may not share entity ids with an older cloud entry:
  registers 3060-3063 hold eight characters, so the 15-character SolisCloud
  inverter serial is unknown unless the user types it.
- SolisCloud samples refresh about every five minutes; faster polling repeats
  the same sample. Lifetime grid import, export and load totals come from
  SolisCloud inverter detail only, so a local-only entry has none.

## Principles

- **Read-only, always.** No path in the integration sends a command to the
  inverter or the logger beyond register reads.
- **Never invent data.** Missing, null, or implausible values are held or
  shown as unavailable, never guessed.
- **Be gentle with the hardware.** One socket, one request in flight, paced
  reads, backoff instead of retry storms; never send a framing a stick did not
  ask for.
- **Stable identity.** Entity and device ids follow the inverter serial and
  a stable key, never the display name or the IP.
