# Examples

The entity IDs below are from the lab plants: a single-phase inverter on the
plant **North Roof** (`sensor.north_roof_s6_gr1p5k_*`) and a hybrid called
**Barn Hybrid** (`sensor.barn_hybrid_*`). Replace them with your own; the
entity settings dialog shows each entity ID.

## Energy dashboard

Settings → Dashboards → Energy. Use the lifetime `total_increasing` sensors.

| Dashboard field | Sensor |
| --- | --- |
| Solar production | Inverter **Energy total**, one per inverter |
| Grid consumption | Inverter **Grid import total** |
| Return to grid | Inverter **Grid export total** |
| Battery: energy going in | **Battery charged total** |
| Battery: energy coming out | **Battery discharged total** |

Do not also add **Energy today** for the same production, or the dashboard
counts the day twice. The grid totals come from SolisCloud. A local-only
entry does not have them; use the plant's **Grid import today** and **Grid
export today** sensors from a SolisCloud entry, or a separate meter.

For the power settings of the Energy dashboard:

- **Grid power** is positive while exporting and negative while importing.
  Home Assistant expects the opposite, so choose the inverted option.
- **Battery power** is positive while charging and negative while
  discharging.

## Only while the inverter is producing

A condition block for any automation that should run only while the inverter
is generating:

```yaml
conditions:
  - condition: state
    entity_id: sensor.north_roof_s6_gr1p5k_status
    state: online
  - condition: numeric_state
    entity_id: sensor.north_roof_s6_gr1p5k_ac_power
    above: 100
```

Do not treat `unavailable` and `0` as the same thing. `unavailable` means the
sample is stale or the poll failed. `0` means a fresh offline reading.

## Notify on an inverter alarm

The alarm binary sensor turns on while SolisCloud has a pending alarm. Its
`code`, `message` and `advice` attributes describe it. They can be missing
when only the inverter status says "alarm", so the template has a fallback.

```yaml
alias: Solis inverter alarm
description: Send the Solis alarm text to my phone.
triggers:
  - trigger: state
    entity_id: binary_sensor.barn_hybrid_alarm
    from: "off"
    to: "on"
conditions: []
actions:
  - action: notify.notify
    data:
      title: "Solis alarm {{ state_attr(trigger.entity_id, 'code') or '' }}"
      message: >-
        {{ state_attr(trigger.entity_id, 'message')
           or 'The inverter reports an alarm.' }}
        {% if state_attr(trigger.entity_id, 'advice') %}
        Advice: {{ state_attr(trigger.entity_id, 'advice') }}
        {% endif %}
mode: single
```

## Notify when the battery is low

```yaml
alias: Solis battery low
description: Warn when the home battery stays below 20 percent.
triggers:
  - trigger: numeric_state
    entity_id: sensor.barn_hybrid_battery
    below: 20
    for:
      minutes: 10
conditions: []
actions:
  - action: notify.notify
    data:
      title: Home battery low
      message: >-
        The battery is at {{ states('sensor.barn_hybrid_battery') }} %.
mode: single
```

## Use an export surplus

Turn on a load (here a water heater switch) after the plant has exported
more than 1500 W for ten minutes, and turn it off again once the house has
been importing for five minutes. Grid power is positive while exporting.

```yaml
alias: Solis export surplus
description: Run the water heater on solar surplus.
triggers:
  - trigger: numeric_state
    entity_id: sensor.barn_hybrid_grid_power
    above: 1500
    for:
      minutes: 10
    id: surplus
  - trigger: numeric_state
    entity_id: sensor.barn_hybrid_grid_power
    below: 0
    for:
      minutes: 5
    id: importing
conditions: []
actions:
  - choose:
      - conditions:
          - condition: trigger
            id: surplus
          - condition: state
            entity_id: sensor.barn_hybrid_status
            state: online
        sequence:
          - action: switch.turn_on
            target:
              entity_id: switch.water_heater
      - conditions:
          - condition: trigger
            id: importing
        sequence:
          - action: switch.turn_off
            target:
              entity_id: switch.water_heater
mode: single
```

SolisCloud updates about every five minutes, so keep the `for:` times at a
few minutes or more. A local stick updates every minute.
