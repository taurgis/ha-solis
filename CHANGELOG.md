# Changelog

## 0.2.0

### Minor Changes

- af2b66f: Initial SolisCloud integration: config flow, coordinator, unit handling, alarms, and a signature-checking mock.
- 1a4ddda: Read a Solis S2-WL-ST on Modbus TCP port 502, auto-detect it beside a Solarman stick, and explain firmware that closes port 502 or answers TCP without Modbus.

### Patch Changes

- b142410: Hold energy totals across null and zero cloud samples, follow daylight saving when the plant offset is within an hour of Home Assistant, and correct migration, retries and the documented minimum Home Assistant version.
- a3829cb: Sign Solis battery power from the charge and discharge components, so positive means charging and negative means discharging.
- 878a141: Hold the last good SolisCloud snapshot through an outage, hide stale live readings per inverter, and keep energy totals from moving backwards when the cloud sends a reset or a spike.
- b957172: Ship the published Solis icon and logo in the integration brand folder so HACS and Home Assistant 2026.3 show them without the brands CDN.
- ccbbaf7: Score the Solis integration against the Home Assistant quality scale, translate update failures, and require every module to stay above 95 percent coverage.
- b944b38: Read a Solarman logger on the LAN, fall back to Modbus TCP when V5 is not the framing, and pause local reads when the stick resets instead of treating that as night.

## 0.1.0

Initial release of the SolisCloud rewrite. See `docs/research/solis-api.md`.
