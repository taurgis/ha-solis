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
  are supported). The local placeholder plant (`station:local`) gets no
  device, so a local inverter has no parent; setup detaches the shared one
  older releases created.
- A local entry's logger device carries the stored stick MAC as a
  `CONNECTION_NETWORK_MAC` connection. Logger and EPM names are translated
  (`Logger {serial}`, `EPM {serial}`) unless the serial already starts with
  that word.
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
- **Availability.** While the inverter is offline (state 2, at any sample
  age) produced quantities read `0` (AC power, string power and current,
  phase current, battery power and current, backup and smart-load power)
  and measured ones read unknown (frequency, temperature, grid and
  consumption power, battery, string and phase voltage, grid-meter phase
  readings). Otherwise instant sensors are `unavailable` when the sample is
  older than 15 min or that inverter's detail failed; status then reads
  `stale`. Energy totals keep their last value through all of these.
- **Appearing and disappearing.** Battery, lifetime grid/load
  (`grid_import_total`, `grid_export_total`, `load_total`, from SolisCloud
  `gridPurchasedTotalEnergy`, `gridSellTotalEnergy`, `homeLoadTotalEnergy`),
  phase and PV-string entities are created once their fields appear and kept
  if a later poll omits them. Phase entities of a single-phase inverter
  (`acOutputType` 0) are disabled by default. A
  device missing from a *complete* poll is removed; a partial poll or an
  unconfirmed empty inverter list keeps it. A complete *local* poll removes
  only logger devices: plants, EPMs and other inverters are SolisCloud's to
  report, so a cloud entry (a migrated one included) that gains a stick or
  switches to `local` keeps them and their entities until a SolisCloud poll
  drops them or the user deletes the device.
- **No invented data.** No integration-derived energy sensors (HA can derive
  them from power sensors) and no `energy_storage` device class — see the
  goal's non-goals.
- **Signs.** Grid power is SolisCloud `psum`: positive exporting, negative
  importing (the reverse of Home Assistant's grid convention; documented,
  not flipped, so existing history keeps its meaning). Battery power is
  positive charging, negative discharging.
- **Precision.** Lifetime energy counters suggest one decimal; period
  counters two.
- **Energy value per poll.** The guarded energy value is computed once per
  coordinator update (and once when added), not on every state read.
- **Icons** come from `icons.json` translation keys (status states, battery
  power by sign, SOH and SOC settings, alarm state); everything else uses
  its device-class icon.
- `PARALLEL_UPDATES = 0`: entities never poll on their own.

## Boundary

- Reads only `coordinator.data`; never calls a transport, HTTP or a socket.
- Uses the plant-day helpers from `cloud-transport`'s `parser`
  (`resolve_zone`, `plant_period_start`).
