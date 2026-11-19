---
"ha-solis-release-tools": minor
---

Live readings follow Home Assistant's availability rules while the inverter is off. What the inverter produces (AC output, PV string power and current, battery power and current, backup and smart-load power) reads 0 whenever SolisCloud reports it offline, even when its last sample is hours old, instead of going unavailable overnight. What it only measures (grid power, house consumption, the grid-meter phase power, apparent and reactive power, grid voltage and frequency, PV and battery voltage, temperature) reads unknown instead of 0, since those keep changing without the inverter. A failed or stale read still makes them unavailable, and energy totals are unchanged. The battery charging binary sensor reads off while the inverter is offline.
