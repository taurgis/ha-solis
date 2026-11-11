# Data updates

SolisCloud's own document says every interface refreshes about every five
minutes. The default poll interval is 300 seconds. The minimum is 60 seconds.
A faster poll repeats the same sample and spends the rate limit.

Each poll is sequential: station list, then for each selected plant the
station detail, the day-energy row and the inverter list, then each inverter
detail, then alarms, collectors and EPMs. Calls to one path are at least two
seconds apart by default.

HTTP 429, timeouts and HTTP 5xx are retried with exponential backoff, jitter
and an 8 second cap. A `Retry-After` header is the minimum wait, including on
a 503. A 408 whose response carries a Date header is signed again at that
clock. Other 4xx responses are not retried. Auth failures, a clock the server
will not correct, and platform code `1` (abnormal data) are not retried.

An HTML page, a truncated body, or a non-JSON content type is a gateway
failure, not a bad API key. HTTP 401 and 403 start reauthentication only when
the body is the Solis auth envelope. DNS and TLS errors are the same kind of
transient failure as a timeout.

After four gateway failures the client opens a circuit and skips the network
until a cooldown of at most five minutes elapses. While an outage continues,
the poll interval backs off with jitter up to 15 minutes and never goes
faster than the configured interval. The first setup with no saved snapshot
raises `ConfigEntryNotReady`, so Home Assistant retries instead of failing
the entry permanently.

A failed inverter list keeps the previous inverters. An empty list is ignored
for two polls before it is believed. One inverter's detail can fail while
the others update; that inverter's live sensors go unavailable and its energy
totals stay. A sample older than the last one kept for that inverter is
ignored. Cached rows for inverters that a later complete poll does not list
are dropped. The same fault is logged once, not on every later poll.

An inverter whose timestamp is older than 15 minutes reports instantaneous
sensors as unavailable and status `stale`. An inverter that is offline in a
fresh sample reports power as 0. Energy totals keep their last good value
across those gaps.
