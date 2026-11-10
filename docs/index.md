# Solis for Home Assistant

SolisCloud monitoring for Home Assistant: plants, inverters, batteries,
alarms, data loggers and export meters.

The integration polls the SolisCloud Platform API v2 with the API key and
secret. It does not send control writes.

Supported devices are any Solis inverter that appears on a SolisCloud
account: grid and hybrid, single phase and three phase, with or without a
battery, plus the plant, the data logger and an export power manager when
the account has them. Control writes are not supported.

- [Installation](installation.md)
- [Configuration](configuration.md)
- [Entities](entities.md)
- [Limitations](limitations.md)
- [Troubleshooting](troubleshooting.md)
- [Research](research/solis-api.md)
