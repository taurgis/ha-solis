---
id: cloud-transport
type: module-design
status: active
title: Cloud transport (SolisCloud Platform API v2)
parent: solis-architecture
references: [solis-api-research]
---

## Responsibility

Read one SolisCloud account through the Platform API v2 and return a
`SolisSnapshot`. Files: `api.py` (HTTP client), `signing.py` (HMAC-SHA1
`Content-MD5` signing), `parser.py` (JSON → snapshot), `units.py` (unit
conversion), and `SolisCloudTransport` plus the `SolisTransport` protocol in
`transport.py`. Endpoint list, signing details, rate limits and field quirks
are recorded in `solis-api-research`; this spec holds the behaviour the code
commits to.

## Behaviour

- **Signing.** The bytes signed are the bytes sent (the body is serialized
  once; aiohttp's `json=` would change spacing and break `Content-MD5`). Dates
  use English day/month names regardless of locale. A 408 whose response
  carries a `Date` header is re-signed at the server's clock.
- **Pacing.** Calls to one path are at least `endpoint_interval` apart
  (default 2 s, below the published 2 calls/s because faster traffic is a
  common cause of HTTP 502). A poll is sequential: station list, then per
  selected plant the detail, day-energy row and inverter list, then each
  inverter detail, then alarms, collectors and EPMs.
- **Retries.** Timeouts, DNS/TLS errors and 5xx retry with exponential
  backoff, jitter and an 8 s cap; `Retry-After` is a floor. A 429 waits
  `Retry-After` (jittered, at most 60 s). Other 4xx,
  auth failures, an uncorrectable clock, and platform code `1` (abnormal
  data) are not retried.
- **Error classification.** HTML, truncated or non-JSON bodies are gateway
  failures, not bad credentials. 401/403 raise `SolisAuthError` only when the
  body is the Solis auth envelope.
- **Circuit breaker.** After four gateway failures the client skips the
  network until a cooldown of at most five minutes.
- **Partial polls.** A failed inverter list keeps the previous inverters; an
  empty list is believed only after two polls. One inverter's failed detail
  leaves the others updating. A sample older than the cached one, or a day
  in the future, is ignored. Rows no longer listed by a complete poll are
  pruned. A repeated fault is logged once at warning, then at debug.
- **Units.** Every power/energy figure is converted with its companion unit
  string; a missing unit falls back to the V2.0 field-table default. Literal
  `"null"`/`"None"` strings are missing values. Known platform typos
  (`bstteryCurrent`, energy labelled `kW`) are handled explicitly.
- **Time.** `dataTimestamp` is a Unix epoch (ms). The plant's numeric offset
  is mapped to an IANA zone: Home Assistant's zone when within an hour of the
  offset (so DST is followed), otherwise the fixed offset, unless the
  `time_zone` option names a zone or `plant`.
- **Battery sign.** Battery power is positive while charging:
  `batteryPowerZheng − batteryPowerFu` when present, otherwise the unsigned
  value negated while `storageBatteryCurrent` is negative.

## Boundary

- Home Assistant–free; depends only on the shared kernel (`models`,
  `errors`, `const`) and aiohttp. The session is injected by
  `integration-setup`.
- Never calls `/v2/api/control` or `/v2/api/atRead`.
- Raises errors from `errors.py` only; policy on what to do with them
  belongs to `coordinator`.
- `parser`'s plant-day helpers are also used by `entities` (see
  architecture's structural debt).
