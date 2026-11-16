---
id: dev-tooling
type: module-design
status: active
title: "Development tooling: mocks, emulators, labs, scripts"
parent: solis-architecture
depends-on: [cloud-transport, local-transport]
---

## Responsibility

Let the integration be developed and checked without a Solis account or the
loggers the maintainer does not own. Nothing here ships in
`custom_components/solis`.

| Tool | Purpose |
| --- | --- |
| `tools/mock_soliscloud` | HTTP stand-in for SolisCloud v2 that verifies the real HMAC signature; a fixture catalog of plants and inverters (single-phase grid, three-phase hybrid with battery, offline, stale); `POST /__control` fault modes (`server_error`, `auth_error`, `clock`, `rate_limit`, `timeout`, `abnormal`); `GET /__expected` for the values the integration should show. |
| `tools/mock_soliscloud/stick.py` | Logger emulator: Solarman V5 on 8899 + UDP 48899, or `--kind s2` Modbus TCP on 502; control hooks for night, closed port, silence, max clients, close-disables-port. |
| `tools/lab` | Dongle lab: one process emulating every logger kind on its own loopback address, and `run_lab.py`, which boots Home Assistant (current and minimum versions) against them and records each scenario. |
| `lab/docker-compose.yml` | Home Assistant next to the mock cloud, for manual testing. |
| `scripts/check_minimum_home_assistant.py` | CI boot of the minimum HA version against the mock: entity values, a short vs sustained 502, single-inverter failure, bad bodies not starting reauth, monotonic lifetime totals, then a local Solarman entry through night and back. |
| `scripts/check_module_coverage.py` | Fails unless every package module is above 95 % coverage. |
| `scripts/read_solis_stick.py` | Read a real stick's string registers from a checkout. |
| `scripts/capture_screenshots.py` | Screenshots of the lab for `docs/screenshots`. |
| `scripts/*.mjs`, `.changeset/` | Changesets versioning: sync the version into `manifest.json`, create the release tag (release workflow only). |

## Decisions

- **Emulators reuse the production codecs.** The mock and the stick import
  `signing`, `parser` and `solarman` from the package, which is why those
  modules must stay Home Assistant–free (architecture invariant). The
  emulators are built from the protocol and from frames captured on a real
  stick, because IGEN publishes no runnable LSW-3 / LSE-3 firmware.
- Emulators are the only verification for most logger kinds (see the goal's
  known limitations); a behaviour change in `local-transport` should come
  with a matching emulator mode.

## Boundary

- May import from `custom_components/solis`; the package never imports from
  `tools/` or `scripts/`.
- Never tags or publishes a release from a pull request.
