# Data updates

SolisCloud's own document says every interface refreshes about every five
minutes. A cloud-only entry polls every 300 seconds by default and never
faster than 60. A local logger defaults to 60 seconds and cannot be faster
than 30. Register reads on the one open socket are a second apart. A faster
cloud poll repeats the same sample and spends the rate limit.

Each poll is sequential: station list, then for each selected plant the
station detail, the day-energy row and the inverter list, then each inverter
detail, then alarms, collectors and EPMs. Calls to one path are at least two
seconds apart by default.

Timeouts and HTTP 5xx are retried with exponential backoff, jitter and an
8 second cap. HTTP 429 waits the `Retry-After` value (with a little jitter,
at most 60 seconds). A `Retry-After` header is also the minimum wait on a
503. A 408 whose response carries a Date header is signed again at that
clock. Other 4xx responses are not retried. Auth failures, a clock the server
will not correct, and platform code `1` (abnormal data) are not retried. A
rejected key starts Home Assistant's reauthentication and stops polling with
that key. Abnormal data fails the update and is logged once; it opens no
repair.

An HTML page, a truncated body, or a non-JSON content type is a gateway
failure, not a bad API key. HTTP 401 and 403 start reauthentication only when
the body is the Solis auth envelope. DNS and TLS errors are the same kind of
transient failure as a timeout.

After four gateway failures the client opens a circuit and skips the network
until a cooldown of at most five minutes elapses. While an outage continues,
the poll interval backs off with jitter up to 15 minutes and never goes
faster than the configured interval. After three failed polls, or once the
last good sample is more than 15 minutes old, live readings are hidden and
energy totals are held. One warning is logged when that starts and one info
line when SolisCloud answers again; no repair is opened. The first setup
with no saved snapshot raises `ConfigEntryNotReady`, so Home Assistant
retries instead of failing the entry permanently.

A local stick is polled on its own interval (60 seconds by default). While the stick is off
with the inverter, the interval backs off from two minutes and stops at 15
minutes. The next successful connect, including one that finds the stick on
a new address, returns to the configured interval. A reset after the TCP
handshake is a different pause: at least 10 seconds, doubling up to about
10 minutes, one attempt each time. That matches the logger TCP idle time
other Solarman projects report (about 300 seconds, sometimes 600). Night,
when there is no route, is still the asleep sample and does not call the
cloud for live power.

A stick that only serves a status web page refreshes its own values about
every five minutes. Reading it every minute only makes a new sample show up
sooner. With SolisCloud as backup, the cloud is read at most every five
minutes, not on every poll.

When a Solarman / IGEN stick gets a new address from DHCP, Home Assistant's
DHCP watcher (MAC prefix `E8:FD:F8` or `BC:54:F9`, or a logger whose MAC is
already stored) updates the entry and reloads it.

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
