# Use cases

SolisCloud already shows the plant in a phone app. In Home Assistant the
same readings can drive the rest of the house.

- Fill the Energy dashboard: inverter **Energy total** as solar production,
  **Grid import total** and **Grid export total** for the grid, and
  **Battery charged total** and **Battery discharged total** for a hybrid's
  battery. See [Examples](examples.md#energy-dashboard).
- Get a notification when the inverter raises an alarm. The alarm binary
  sensor carries the Solis alarm code, message and advice.
- Warn when the battery runs low, or start a load (dishwasher, car charger,
  water heater) when the plant is exporting a surplus.
- Run an automation only while status is `online` and AC power is above a
  threshold. `0` means a fresh offline sample. `unavailable` means the
  sample is stale or the last poll failed.
- Keep reading the inverter during an internet outage by using the local
  stick, with SolisCloud as a backup when the stick is busy.

Example automations are in [Examples](examples.md).
