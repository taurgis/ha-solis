# Solis for Home Assistant

SolisCloud monitoring for Home Assistant: plants, inverters, batteries,
alarms, data loggers and export meters.

The integration polls the SolisCloud Platform API v2 with the API key and
secret, and can read a Solarman logger or a Solis S2-WL-ST on the LAN while
the inverter is producing. It does not send control writes.

Supported devices are any Solis inverter that appears on a SolisCloud
account: grid and hybrid, single phase and three phase, with or without a
battery, plus the plant, the data logger and an export power manager when
the account has them. Control writes and the legacy Ginlong portal API are
not supported.

Once the plants are in Home Assistant you can put lifetime production and
battery energy on the energy dashboard, automate on the alarm binary sensor,
and tell a fresh offline reading (`0`) from a failed poll (`unavailable`).
See [Use cases](use-cases.md).

- [Installation](installation.md)
- [Configuration](configuration.md)
- [Entities](entities.md)
- [Limitations](limitations.md)
- [Troubleshooting](troubleshooting.md)
- [Research](research/solis-api.md)
- [S2-WL-ST local reads](research/s2-wl-st.md)
- [Dongles and gateways](research/dongles.md)
