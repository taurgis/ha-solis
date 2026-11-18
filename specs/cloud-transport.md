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
  selected plant the detail, the day-energy row (only when the detail lacks
  one of today's generation, import, export or load) and inverter list, then each
  inverter detail, then alarms, collectors and EPMs.
- **Retries.** Timeouts, DNS/TLS errors and 5xx retry with exponential
  backoff, jitter and an 8 s cap; `Retry-After` is a floor. A 429 waits
  `Retry-After` (jittered, at most 60 s). Other 4xx,
  auth failures, an uncorrectable clock, and platform code `1` (abnormal
  data) are not retried.
- **Error classification.** HTML, truncated or non-JSON bodies are gateway
  failures, not bad credentials. A 401/403 raises `SolisAuthError` unless its
  body is HTML (gateway or WAF page → `SolisServerError`); SolisCloud answers
  a wrong secret with a bare 403, not `R0000`. A JSON body fails when `code`
  is not `"0"` or `success` is `false`; the message is `msg`, else `message`.
- **Circuit breaker.** After four gateway failures the client skips the
  network until a cooldown of at most five minutes.
- **Partial polls.** A failed inverter list keeps the previous inverters; an
  empty list is believed only after two polls. One inverter's failed detail
  leaves the others updating. A sample older than the cached one, or a day
  in the future, is ignored. Rows no longer listed by a complete poll are
  pruned. A repeated fault is logged once at warning, then at debug. An
  inverter detail whose `sn` differs from the list row (SolisCloud's
  placeholder for an unknown serial: zeros, `dataTimestamp: "0"`) is
  rejected like a failed detail, and the list serial is always the identity.
- **Units.** Every power/energy figure is converted with its companion unit
  string and its own `*Pec` scale factor; a missing unit falls back to the
  V2.0 field-table default (kW for power, kWh for energy). Celsius is
  accepted as `C`, `°C` or `℃` (`*Str` or `*Unit`). Literal
  `"null"`/`"None"` strings are missing values. Known platform typos
  (`bstteryCurrent`, energy labelled `kW`) are handled explicitly.
- **Time.** `dataTimestamp` is a Unix epoch (ms). The plant's zone for
  "today" is, in order: the `time_zone` option when it names a zone (`plant`
  keeps the fixed numeric offset); the plant's `timeZoneStandardId` when it
  loads and is within an hour of the numeric offset; Home Assistant's zone
  when within an hour of the offset (so DST is followed); otherwise the fixed
  offset. The transport loads the plant zone in the executor before parsing,
  so nothing reads tzdata in the event loop.
- **Battery sign.** Battery power is positive while charging, negative while
  discharging. `batteryPowerZheng`/`batteryPowerFu` are watts with no unit
  field; when their difference is non-zero it gives the sign and
  `batteryPower` (with its `Str`/`Pec`) the magnitude, falling back to
  Zheng − Fu in watts. Without a split, an unsigned value is negated while
  `storageBatteryCurrent` is negative. Published unchanged from upstream.
- **Grid sign.** `psum` (grid power) is negative while importing and positive
  while exporting. It is published as-is for upstream compatibility, which
  is the opposite of Home Assistant's "positive = import".
- **Lifetime totals.** Grid import/export and load lifetime counters are read
  per inverter from inverter detail (`gridPurchasedTotalEnergy`,
  `gridSellTotalEnergy`, `homeLoadTotalEnergy`), as upstream did.

## Boundary

- Home Assistant–free; depends only on the shared kernel (`models`,
  `errors`, `const`), aiohttp and the standard library. The session is injected by
  `integration-setup`.
- Never calls `/v2/api/control` or `/v2/api/atRead`.
- Raises errors from `errors.py` only; policy on what to do with them
  belongs to `coordinator`.
- `parser`'s plant-day helpers are also used by `entities` (see
  architecture's structural debt).
