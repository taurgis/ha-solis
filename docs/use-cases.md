# Use cases

SolisCloud already shows the plant in a phone app. In Home Assistant the
same readings can drive the rest of the house.

- Put inverter **Energy total** on the energy dashboard as solar production,
  next to grid and battery sensors from the rest of the home.
- Use **Battery charged total** and **Battery discharged total** so the
  dashboard can see what the hybrid stored and released.
- Notify when the alarm binary sensor turns on. The state attributes carry
  the Solis alarm code, message, and advice.
- Run an automation only while status is `online` and AC power is above a
  threshold. `0` means a fresh offline sample. `unavailable` means the
  sample is stale or the last poll failed.
- Keep yesterday's production from being wiped when the cloud omits a field.
  A null or zero inside the current plant day stays at the last good reading.
