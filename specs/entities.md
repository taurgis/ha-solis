---
id: entities
type: module-design
status: active
title: Entities and devices
parent: solis-architecture
depends-on: [coordinator, legacy-migration, cloud-transport]
---

## Responsibility

Turn the coordinator's `SolisSnapshot` into Home Assistant devices and
entities. Files: `entity.py` (base entity, device builders), `sensor.py`,
`binary_sensor.py` (alarm, battery charging), `energy.py` (energy
sanitising). The user-facing catalogue of entities, units and state classes
is `docs/entities.md`; this spec holds the rules behind it.

## Devices and identity

- Devices: plant `station:{id}`, inverter `inverter:{serial}`, logger
  `collector:{serial}`, EPM `epm:{serial}`. Inverters, loggers and EPMs hang
  off their plant (`via_device`, or `via_device_id` from HA 2026.8 — both
  are supported).
- Entity unique ids are `{serial}_{key}`; `has_entity_name` is on.
- Legacy ids are migrated by `legacy-migration` before entities are added.

## Rules

- **Energy is monotonic.** Energy sensors are `total_increasing`, kWh, no
  `last_reset`. A null, zero or decrease inside the current plant day, month,
  year or lifetime keeps the previous reading; a period counter may fall only
  when its period rolls over on the plant-local boundary. Non-finite,
  negative, > 10 GWh values and lifetime jumps > 100 000 kWh are dropped.
  The last total is restored across restarts (`RestoreSensor`) and held to
  the same rules.
- **Availability.** Instant sensors read `0` when the inverter is offline in
  a fresh sample, and `unavailable` when the sample is older than 15 min or
  that inverter's detail failed; status then reads `stale`. Energy totals
  keep their last value through both.
- **Appearing and disappearing.** Battery, phase and PV-string entities are
  created once their fields appear and kept if a later poll omits them. A
  device missing from a *complete* poll is removed; a partial poll or an
  unconfirmed empty inverter list keeps it.
- **No invented data.** No integration-derived energy sensors (HA can derive
  them from power sensors) and no `energy_storage` device class — see the
  goal's non-goals.
- `PARALLEL_UPDATES = 0`: entities never poll on their own.

## Boundary

- Reads only `coordinator.data`; never calls a transport, HTTP or a socket.
- Uses the plant-day helpers from `cloud-transport`'s `parser`
  (`resolve_zone`, `plant_period_start`).
