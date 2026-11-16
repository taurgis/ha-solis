---
id: legacy-migration
type: module-design
status: active
title: Migration from hultenvp/solis-sensor
parent: solis-architecture
references: [solis-api-research]
---

## Responsibility

Carry a hultenvp/solis-sensor 4.x installation over without losing
configuration or recorder history. The registry rewrites live in
`migration.py`; the config-entry data conversion (`migrate_entry_data`) is
in `config_flow.py` and is driven by `async_migrate_entry` in `__init__.py`.
The upstream id formats and why the domain stays `solis` are in
`solis-api-research`.

## Behaviour

- **Config entries.** Version 1 (solis-sensor 4.x: `portal_domain`,
  `portal_key_id`, `portal_secret`, `portal_plant_id`, `refresh_ok`,
  `request_timeout`) becomes version 2; the portal password is dropped on
  purpose. Version 3 adds `source` (cloud for migrated entries). An entry
  that had the control API enabled gets the fixable `legacy_control` repair
  explaining that control is not carried over; schedules already on the
  inverter are left alone.
- **Entity ids.** Unique ids `{serial}{config name} {sensor label}` (spaces
  as underscores, English labels from upstream's `SENSOR_TYPES`) become
  `{serial}_{key}`, including phase and PV-string sensors. Plant import,
  export and load move onto the plant device and keep their entity id. If a
  new entity already exists beside the legacy one, the legacy entity id is
  kept and the duplicate removed.
- **Device ids.** `{serial}_SolisCloud`-style devices are retargeted to
  `inverter:{serial}`; if the new device already exists, the legacy one is
  detached from the entry so a partial migration leaves one device.
- **Placeholder serials.** A local entry that only knew `L` + logger serial
  is moved onto the real inverter serial once SolisCloud or the serial
  registers supply it (`async_adopt_inverter_serial`).
- **Late inverters.** Migration runs again when an inverter missing at
  startup appears later.

## Boundary

- Touches only the entity and device registries and entry data; never the
  recorder.
- Called from `integration-setup` (entry setup and migration) and from the
  entity platforms in `entities` before entities are added.
