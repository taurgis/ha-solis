# Entities

Each plant, inverter, data logger and export power manager is its own device.
Inverters are linked to their plant. Hybrid inverters are those Solis marks
with product model `2`, or any inverter that reports a battery field.

Energy sensors use `device_class: energy`, `state_class: total_increasing`
and unit kWh. They do not publish `last_reset`. A null, a zero or a decrease
inside the current plant day, month, year or lifetime keeps the previous
reading. A daily, monthly or yearly counter may fall only when that period
rolls. Lifetime totals also ignore a jump of more than 100 000 kWh.

Instantaneous power is 0 when the inverter is offline and the sample is
fresh. It is unavailable when `dataTimestamp` is older than 15 minutes.
Status then reads `stale`. A poll that fails after retries marks every entity
for the entry unavailable until the next success.

## Inverter

| Sensor | Unit | State class | Notes |
| --- | --- | --- | --- |
| Status | | | online, offline, alarm, stale, unknown |
| AC power | W | measurement | 0 when freshly offline |
| Frequency | Hz | measurement | |
| Temperature | °C | measurement | |
| Energy today / this month / this year | kWh | total_increasing | Resets on the plant-local boundary |
| Energy total | kWh | total_increasing | Monotonic |
| Measurement time | | | Diagnostic, disabled by default |
| Grid power | W | measurement | Signed |
| Consumption power | W | measurement | |
| Phase {n} voltage / current / power | V, A, W | measurement | One phase when `acOutputType` is 0, otherwise three |
| String {n} voltage / current | V, A | measurement | |
| String {n} power | W | measurement | Disabled by default |

## Battery

Added when the inverter reports a battery, and kept if a later poll omits the
fields. Battery power is negative when discharging.

| Sensor | Unit | State class | Notes |
| --- | --- | --- | --- |
| Battery power | W | measurement | |
| Battery | % | measurement | State of charge |
| Battery state of health | % | measurement | Diagnostic, disabled by default |
| Battery voltage / current | V, A | measurement | |
| Battery charged / discharged today | kWh | total_increasing | |
| Battery charged / discharged total | kWh | total_increasing | Monotonic |
| Force charge SOC | % | measurement | Diagnostic, disabled by default |
| Overdischarge SOC | % | measurement | Diagnostic, disabled by default |

## Plant

| Sensor | Unit | State class |
| --- | --- | --- |
| Energy today | kWh | total_increasing |
| Grid import today | kWh | total_increasing |
| Grid export today | kWh | total_increasing |
| Load today | kWh | total_increasing |

There is no lifetime grid import or export in the endpoints this integration
polls. The energy dashboard can use the daily `total_increasing` sensors.
Prefer an inverter **Energy total** for solar production.

## Other devices

| Device | Sensor | Notes |
| --- | --- | --- |
| Data logger | Signal strength (dBm) | Diagnostic, disabled by default |
| Export power manager | Power (W) | |
| Inverter | Alarm | Binary sensor, on while a SolisCloud alarm is pending. Attributes: code, message, advice |

An inverter, logger or export meter that disappears from a complete poll is
removed. A partial poll, or an empty inverter list that has not repeated,
keeps the previous devices.
