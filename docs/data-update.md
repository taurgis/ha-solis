# Data updates

SolisCloud's own document says every interface refreshes about every five
minutes. The default poll interval is 300 seconds. The minimum is 60 seconds.
A faster poll repeats the same sample and spends the rate limit.

Each poll is sequential: station list, then for each selected plant the
station detail, the day-energy row and the inverter list, then each inverter
detail, then alarms, collectors and EPMs. Calls to one path are at least two
seconds apart by default.

A failed poll marks entities unavailable and is logged. A failed inverter list
keeps the previous inverters. An inverter whose timestamp is older than 15
minutes reports instantaneous sensors as unavailable and status `stale`.
An inverter that is offline in a fresh sample reports power as 0.
