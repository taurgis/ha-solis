# Examples

## Energy dashboard

Use lifetime `total_increasing` sensors where the cloud provides them.

- Solar production: inverter **Energy total**. One sensor per inverter. Do
  not also add **Energy today** for the same production, or the dashboard
  counts the day twice.
- Battery charge and discharge: **Battery charged total** and **Battery
  discharged total** on the hybrid.
- Grid import and export: the plant **today** sensors. The cloud endpoints
  used here do not publish a lifetime import or export. `total_increasing`
  treats the midnight drop as a reset.

**Energy today** on the inverter is the same kind of daily counter. It is
useful on a dashboard card. It is a poor second source for the energy
dashboard's solar total.

## Automation

An automation that only runs when the inverter is actually generating:

```yaml
condition:
  - condition: state
    entity_id: sensor.north_roof_s6_gr1p5k_status
    state: online
  - condition: numeric_state
    entity_id: sensor.north_roof_s6_gr1p5k_ac_power
    above: 100
```

Do not treat `unavailable` and `0` as the same thing. `unavailable` means the
sample is stale or the poll failed. `0` means a fresh offline reading.
