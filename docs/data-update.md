# Data updates

SolisCloud's own document says every interface refreshes about every five
minutes. The default poll interval is 300 seconds. The minimum is 60 seconds.
A faster poll repeats the same sample and spends the rate limit.

Each poll is sequential: station list, then for each selected plant the
station detail, the day-energy row and the inverter list, then each inverter
detail, then alarms, collectors and EPMs. Calls to one path are at least two
seconds apart by default.

HTTP 429, timeouts and HTTP 5xx are retried with backoff. A 408 whose
response carries a Date header is signed again at that clock. Other 4xx
responses are not retried. Auth failures, a clock the server will not
correct, and platform code `1` (abnormal data) are not retried.

A poll that still fails marks entities unavailable and is logged once per
failure, not on every later poll of the same failure. A failed inverter list
keeps the previous inverters. An empty list is ignored for two polls before
it is believed. Detail failures reuse the last detail. Cached rows for
inverters that a later complete poll does not list are dropped.

An inverter whose timestamp is older than 15 minutes reports instantaneous
sensors as unavailable and status `stale`. An inverter that is offline in a
fresh sample reports power as 0. Energy totals keep their last good value
across those gaps.
