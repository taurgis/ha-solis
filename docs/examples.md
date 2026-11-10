# Examples

Energy dashboard:

- Solar production: the inverter `Energy total` sensor, or the plant
  `Energy today` sensor if you prefer the plant aggregate.
- Grid export: plant `Grid export today` is a daily total. For a lifetime
  counter, use a utility meter fed by an inverter that reports export.
- Battery charge and discharge: `Battery charged total` and
  `Battery discharged total` on the hybrid.

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
