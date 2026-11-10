# Entities

Each plant, inverter, data logger and export power manager is its own device.

Inverter sensors include status, AC power, frequency, temperature, energy
today / month / year / total, grid power, consumption power, phase voltage
and current, and PV string voltage and current. Hybrid inverters also expose
battery power (negative when discharging), state of charge, charge and
discharge energy, and the force-charge and overdischarge SOC setpoints as
diagnostic sensors.

Plant sensors are energy today, grid import today, grid export today and
load today. Daily energies use `total` with `last_reset` at plant-local
midnight. Lifetime energies use `total_increasing`.

A problem binary sensor is on while SolisCloud has a pending alarm for that
inverter.

Instantaneous power is 0 when the inverter is offline and the sample is
fresh. It is unavailable when the sample is older than 15 minutes. A failed
poll makes every entity for the entry unavailable until the next success.
