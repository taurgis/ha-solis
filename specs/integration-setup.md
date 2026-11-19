---
id: integration-setup
type: module-design
status: active
title: Integration setup, config flow and diagnostics
parent: solis-architecture
depends-on: [cloud-transport, local-transport, coordinator, legacy-migration]
---

## Responsibility

The Home Assistant face of the integration: entry lifecycle, user-facing
flows and diagnostics. Files: `__init__.py`, `config_flow.py`,
`diagnostics.py`, `strings.json` /
`translations/` (en, nl), `manifest.json`, `quality_scale.yaml`. Field-level
documentation for users is `docs/configuration.md`.

## Entry lifecycle

- **Setup** builds the transport from entry data (see the architecture's
  transport modes), creates the coordinator, runs the first refresh, migrates
  devices and legacy entity ids, detaches the shared `station:local`
  placeholder plant older releases registered, then forwards the `sensor`
  and `binary_sensor` platforms. Any failure in the first refresh closes the
  transport; `ConfigEntryAuthFailed` passes through as is, anything else
  that is not ready is `ConfigEntryNotReady`.
- **Unload** closes the transport. Open repairs stay until their condition
  clears; **removal** deletes the stored snapshot and every repair of the
  entry.
- **No update listener.** The options flow is `OptionsFlowWithReload`, and
  reconfigure and reauth reload through `async_update_reload_and_abort`.
  Runtime writes to entry data (below) therefore never reload the entry,
  except an adopted inverter serial, which schedules one reload so entities
  are rebuilt under the new serial.
- **Device removal** is allowed only for devices the latest snapshot no
  longer contains.
- **Entry migration** to the current version is delegated to
  `legacy-migration`.
- Values a transport learns at runtime (new logger host/MAC, framing,
  inverter serial) are written back to entry data.

## Flows

- **User flow.** The first step is a menu: `cloud` (SolisCloud only),
  `local` (local stick only) or `both` (local stick with SolisCloud backup).
  Cloud asks for API URL, key id and secret, then the plants to poll; `both`
  continues into the logger step after the plants. The `logger` step asks
  for the logger kind (Auto-detect by default), address (a dropdown of
  sticks found by UDP discovery, custom values allowed) and an optional
  inverter serial; logger serial, port, slave and the RS485 gateway preset
  sit in a collapsed Advanced section. The flow probes the logger and stores
  what the probe found plus a detection code. When port 502 answers and the
  web page does not name the device, `logger_choice` asks S2-WL-ST or
  gateway. The `logger_password` step appears only when a web-page stick
  rejected the saved or factory password (blank means the factory
  password); on Reconfigure a blank field keeps the saved one for the same
  host.
- **URL normalisation.** Public `http://` is stored as `https://`; loopback,
  private addresses and single-label hosts may stay `http`.
- **Uniqueness.** The API key id is the unique id of a cloud-only entry; a
  logger's identity (see `local-transport`) is the unique id of an entry
  with a logger, and Reconfigure refuses an id another entry holds. The same
  key on a second entry is not blocked when either entry has a logger.
  The same key or stick cannot be added twice; a duplicate updates the host.
- **Reauth** `reauth_confirm` replaces the key id and secret and refuses a
  key from another account. A new key id (a rotated key) is the same account
  when it still sees every plant the entry reads; a SolisCloud-only entry
  then takes the new key id as its unique id. Reconfigure moves the unique id
  of a SolisCloud-only entry with its key id the same way.
  `reauth_web` replaces the web password of a web-page stick. An entry with
  both a key and a web-page stick gets a `reauth` menu to pick which.
- **Reconfigure** is a menu: `reconfigure_cloud` (URL, key; on a local entry
  it adds SolisCloud and becomes `both` after choosing plants), `stations`
  (not on a local entry), `logger` (the logger step prefilled; a cloud entry
  that gains a logger becomes `both`, and a poll interval still on the cloud
  default of 300 s becomes the local default of 60 s), and `connection` when the entry has
  both a key and a logger host (switch `cloud` / `local` / `both`; the
  other source's fields stay). Entities are not deleted. A logger id another
  entry already uses is refused.
- **Options** hold poll interval and plant timezone (a dropdown of `auto`
  and `plant` that also takes an IANA name), with request timeout and
  endpoint gap in a collapsed Advanced section. They never ask for the
  logger address. Saving reloads the entry.
- **DHCP** (`manifest.json`: OUIs `E8FDF8*` and `BC54F9*`, and
  `registered_devices`) acts only on private addresses. It updates the host
  of an entry whose stored MAC matches (never a web-page stick entry,
  because that entry sends a password to its host), and records the MAC on
  an entry already at that IP. A new Solarman stick that answers the UDP
  probe gets its serial as unique id and a `discovery_confirm` menu (`local`
  or `both`) that continues into the normal steps.
- `config_flow.py` is held to 100 % test coverage.

## Repairs and diagnostics

- There are no fix-flows. Every repair is non-fixable; poll-fault repairs
  are opened and cleared by `coordinator`, and `legacy_control` is created
  once during migration as a persistent issue. The gateway, framing, S5 and
  legacy-control repairs carry a `learn_more_url` to an anchor in
  `docs/troubleshooting.md`.
- Diagnostics include the snapshot and the local session (kind, protocol,
  port, slave, register map, experimental flag, detection code with its
  sentence). Key id, secret, portal credentials, the logger web password,
  location, host, MAC, serials, plant ids, names, the entry title and the
  unique id are redacted.

## Boundary

- The only module that constructs transports and the coordinator.
- Contains no protocol logic; probing and form parsing helpers come from
  `local-transport`, station listing from `cloud-transport`.
