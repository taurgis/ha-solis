# Entities

## Devices

- **Plant**: one device per SolisCloud plant. Inverters, loggers and export
  meters are linked to it. A local-only entry has no plant device; its
  inverter stands on its own.
- **Inverter**: manufacturer, model, serial number and firmware. Hybrid
  inverters are those Solis marks as product model `2`, or any inverter that
  reports a battery field.
- **Data logger**: named `Logger <serial>`. On a local entry the logger
  device also carries the stick's MAC address, so Home Assistant can match it
  with the same device seen by your router integration or by DHCP.
- **Export power manager (EPM)**: when the account has one.

An inverter, logger or export meter that disappears from a complete poll is
removed. A partial poll, or an empty inverter list that has not repeated,
keeps the previous devices. You can also delete a device yourself when the
latest poll no longer contains it.

## Inverter

| Sensor | Unit | State class | Notes |
| --- | --- | --- | --- |
| Status | | | online, offline, alarm, stale, unknown |
| Data source | | | Diagnostic. `local`, `cloud`, `asleep`, or `degraded` (the local stick is paused; last values held, or SolisCloud used) |
| AC power | W | measurement | 0 when freshly offline |
| Frequency | Hz | measurement | |
| Temperature | °C | measurement | |
| Energy today / this month / this year | kWh | total_increasing | Resets at the plant's local day, month and year boundary |
| Energy total | kWh | total_increasing | Lifetime production. Never decreases |
| Grid import total | kWh | total_increasing | Lifetime, from SolisCloud. Only when the inverter reports it |
| Grid export total | kWh | total_increasing | Lifetime, from SolisCloud. Only when the inverter reports it |
| Load total | kWh | total_increasing | Lifetime house consumption, from SolisCloud. Only when the inverter reports it |
| Measurement time | | | Diagnostic, disabled by default |
| Grid power | W | measurement | Positive = exporting, negative = importing (see below) |
| Consumption power | W | measurement | |
| Phase {n} voltage / current / power | V, A, W | measurement | One phase on a single-phase inverter, three otherwise. Disabled by default on a single-phase inverter, where they repeat AC power, voltage and current |
| String {n} voltage / current | V, A | measurement | |
| String {n} power | W | measurement | Disabled by default |
| Alarm | | | Binary sensor. On while a SolisCloud alarm is pending or the inverter reports an alarm state. Attributes `code`, `message` and `advice` while an alarm is pending |

Lifetime counters (Energy total, the grid and load totals, and the battery
totals) are shown with one decimal. Daily, monthly and yearly counters are
shown with two.

The grid import, grid export and load totals replace solis-sensor's "Total
Energy Purchased", "Total On-grid Energy" and "Total Energy Used". Their old
entity IDs and history are kept when you migrate.

## Battery

Added when the inverter reports a battery, and kept if a later poll omits the
fields.

| Sensor | Unit | State class | Notes |
| --- | --- | --- | --- |
| Battery power | W | measurement | Positive = charging, negative = discharging. The icon shows the direction |
| Battery charging | | | Binary sensor, on while battery power is positive |
| Battery | % | measurement | State of charge |
| Battery state of health | % | measurement | Diagnostic, disabled by default |
| Battery voltage / current | V, A | measurement | |
| Battery charged / discharged today | kWh | total_increasing | |
| Battery charged / discharged total | kWh | total_increasing | Lifetime. Never decreases |
| Force charge SOC | % | measurement | Diagnostic, disabled by default |
| Overdischarge SOC | % | measurement | Diagnostic, disabled by default |

## Plant

| Sensor | Unit | State class |
| --- | --- | --- |
| Energy today | kWh | total_increasing |
| Grid import today | kWh | total_increasing |
| Grid export today | kWh | total_increasing |
| Load today | kWh | total_increasing |

## Other devices

| Device | Sensor | Notes |
| --- | --- | --- |
| Data logger | Signal strength (dBm) | Diagnostic, disabled by default |
| Export power manager | Power (W) | |

## Sign conventions

- **Grid power** comes from SolisCloud's `psum`: positive while exporting to
  the grid, negative while importing. Home Assistant's grid convention is the
  opposite (positive = importing). In the Energy dashboard power settings,
  choose the inverted option for this sensor.
- **Battery power** is positive while charging and negative while
  discharging. When SolisCloud sends separate charge and discharge values
  (`batteryPowerZheng`, `batteryPowerFu`), they decide the sign. Otherwise an
  unsigned value takes its sign from the battery current.

## What a local stick provides

The local readings depend on the register map of the inverter:

- **Grid (string) inverters** (3000-range map): AC power, frequency,
  temperature, status, energy today, month, year and total, phases and PV
  strings.
- **Hybrid inverters** (33000-range map): AC power, frequency, temperature,
  status, energy today, month, year and total, phases, PV strings, battery
  power, state of charge and health, voltage, current, charged and
  discharged today and total, and consumption power. Grid power is not read
  locally (the register there is the inverter's AC port, not the meter).
- **Web page sticks**: AC power, temperature, energy today and total, and
  the alarm word.

With SolisCloud as backup, SolisCloud fills the rest. The lifetime grid and
load totals, force-charge SOC and the plant sensors come only from
SolisCloud.

## Values and availability

Energy sensors use `device_class: energy`, `state_class: total_increasing`
and unit kWh. They do not publish `last_reset`. A null, a zero or a decrease
inside the current plant day, month, year or lifetime keeps the previous
reading. A daily, monthly or yearly counter may fall only when that period
rolls over. Lifetime totals also ignore a jump of more than 100 000 kWh.
This holds across a restart, which restores the last total.

Instantaneous power is 0 when the inverter is offline and the sample is
fresh. It is unavailable when the measurement time is older than 15 minutes,
or when that inverter's detail call failed while other devices still
updated. Status then reads `stale`.

A short SolisCloud blip (fewer than three failed polls, and the last good
sample under 15 minutes old) keeps showing the last values. A longer outage
hides live readings and leaves energy totals at the last good value until a
poll works again.

Home Assistant 2026.11 can derive energy statistics from a power sensor.
AC power, grid power, consumption power and battery power already use
`device_class: power` and `state_class: measurement`, so the Energy dashboard
can offer that without a second set of sensors here.
