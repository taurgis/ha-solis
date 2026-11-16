---
id: integration-setup
type: module-design
status: active
title: Integration setup, config flow, repairs and diagnostics
parent: solis-architecture
depends-on: [cloud-transport, local-transport, coordinator, legacy-migration]
---

## Responsibility

The Home Assistant face of the integration: entry lifecycle, user-facing
flows, repair fix-flows and diagnostics. Files: `__init__.py`,
`config_flow.py`, `repairs.py`, `diagnostics.py`, `strings.json` /
`translations/` (en, nl), `manifest.json`, `quality_scale.yaml`. Field-level
documentation for users is `docs/configuration.md`.

## Entry lifecycle

- **Setup** builds the transport from entry data (see the architecture's
  transport modes), creates the coordinator, runs the first refresh, migrates
  devices, then forwards the `sensor` and `binary_sensor` platforms. A failed
  first refresh closes the transport; an auth failure becomes
  `ConfigEntryAuthFailed`, anything else `ConfigEntryNotReady`.
- **Unload** closes the transport and deletes the poll-fault repairs.
- **Options changes reload** the entry.
- **Device removal** is allowed only for devices the latest snapshot no
  longer contains.
- **Entry migration** to the current version is delegated to
  `legacy-migration`.
- Values a transport learns at runtime (new logger host/MAC, framing,
  inverter serial) are written back to entry data.

## Flows

- **User flow.** Choose connection `cloud` / `local` / `both`. Cloud asks for
  API URL, key id and secret, then the plants to poll. Local asks for the
  logger kind (Auto-detect by default), address, optional port, slave,
  serials, then probes the logger and stores what the probe found plus a
  detection code.
- **URL normalisation.** Public `http://` is stored as `https://`; loopback,
  private addresses and single-label hosts may stay `http`.
- **Uniqueness.** The API key id is the unique id of a cloud entry; a
  logger's identity (see `local-transport`) is the unique id of a local one.
  The same key or stick cannot be added twice; a duplicate updates the host.
- **Reauth** replaces the key id and secret and refuses a key from another
  account.
- **Reconfigure** changes URL, plants, polling numbers, timezone, and the
  logger address/port/kind without deleting entities; a cloud entry can gain
  a logger here.
- **Options** hold poll interval, request timeout, endpoint gap and plant
  timezone only; they never ask for the logger address.
- **DHCP** (`manifest.json`: OUI `BC54F9*` and known gateway hostnames)
  never creates an entry: it updates the host of an entry whose stored MAC
  matches, and records a MAC for a gateway entry already at that IP.
- `config_flow.py` is held to 100 % test coverage.

## Repairs and diagnostics

- Fix-flows: `invalid_auth` starts reauth; `legacy_control` and the logger
  gateway / framing repairs are fixable confirmations. Poll-fault repairs are
  *opened* by `coordinator`.
- Diagnostics include the snapshot and the local session (kind, protocol,
  port, slave, register map, experimental flag, detection code with its
  sentence). Key id, secret, portal credentials, location, host, MAC and
  serials are redacted.

## Boundary

- The only module that constructs transports and the coordinator.
- Contains no protocol logic; probing and form parsing helpers come from
  `local-transport`, station listing from `cloud-transport`.
