---
id: coordinator
type: module-design
status: active
title: Polling coordinator
parent: solis-architecture
depends-on: [local-transport]
---

## Responsibility

`SolisCoordinator` (`coordinator.py`) is the one place that calls a
transport. It turns transport outcomes into what entities show, decides the
poll interval, opens and clears repairs raised by polling, and persists the
last good snapshot.

## Behaviour

- **Poll interval.** Cloud defaults to 300 s, never faster than 60. Local
  defaults to 60 s, never faster than 30. During an outage the interval
  backs off with jitter up to 15 min and never goes faster than the
  configured one; while a logger sleeps it backs off from 1 min to 15 min.
  The next success restores the configured interval.
- **Error policy** (by class, see the architecture's snapshot contract):
  - auth → open the `invalid_auth` repair and raise `ConfigEntryAuthFailed`,
    so Home Assistant starts reauth and stops polling with the rejected key;
    a local-plus-cloud entry whose local read works still publishes, and the
    transport's `cloud_error` opens the same repair (and one reauth) or the
    clock repair beside the data;
  - clock / abnormal data / no inverters → fail the update with a repair;
  - transient cloud errors → hold the last snapshot;
  - logger busy → hold as degraded; a lock lasting 10 min opens a repair;
  - logger busy with reason `web_auth` or `web_page` (web-page stick: password
    rejected, or no usable page) → the same hold, and the `logger_web` repair
    after 10 minutes;
  - logger offline → asleep snapshot (live power 0, totals kept, no repair),
    or the gateway / closed-port / S5 repairs for faults a person can fix.
- **Blip vs outage.** Fewer than three failed polls with a last good snapshot
  under 15 min old keep the last values. Longer, live readings are hidden,
  energy totals stay, and the `cloud_outage` repair opens; it clears on the
  next real success. A poll with no live inverter counts as a failure.
- **Persistence.** The last good snapshot is stored per entry
  (`solis.snapshot.<entry_id>`), written at most every 5 minutes and once at
  shutdown to spare flash storage, and deleted when the entry is removed, so a restart at night or during an outage
  restores totals instead of failing setup. With no stored snapshot the first
  refresh raises `ConfigEntryNotReady` so Home Assistant retries.
- **Logging.** Each failure kind is logged once, not every poll.

## Boundary

- Depends on the transport protocol, the shared kernel, and — for the night
  snapshot and diagnostics text — `local-transport` (`asleep_snapshot`,
  `detection_why`).
- Owns repair *creation* for poll faults; fix-flows live in
  `integration-setup` (`repairs.py`).
- Knows nothing about entity descriptions or devices.
