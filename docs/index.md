# Solis for Home Assistant

- Reads Solis inverters, batteries and plants from the SolisCloud API, from
  the data logger stick on your home network, or from the stick with
  SolisCloud as a backup.
- Read-only. It never changes inverter settings or battery schedules.
- Energy dashboard ready: lifetime production, battery, grid import and
  export, and house load are `total_increasing` energy sensors.

Solis inverters read through SolisCloud or a supported local stick are
supported: grid and hybrid, single phase and three phase, with or without
a battery, plus the plant, the data logger and an export power manager
when the account has them. The legacy Ginlong portal API and inverter
control are not supported.

This project is not affiliated with or endorsed by Ginlong (Ningbo)
Technologies or Solis.

## Which connection should I pick?

| Your stick | Pick | Notes |
| --- | --- | --- |
| Solarman / IGEN stick: DLS-W, DLS-L, LSW-3, LSE-3 | Local stick (or Local stick with SolisCloud backup) | Found automatically on your network while the inverter is producing. |
| Solis S2-WL-ST (Wi-Fi + LAN) | Local stick, after Modbus TCP is enabled on the stick firmware | Solis's own guide says enabling Modbus TCP disconnects the stick from SolisCloud. Some firmware builds are reported to allow both. See [S2-WL-ST local reads](research/s2-wl-st.md). |
| Solis S3-class Wi-Fi stick whose only local service is a status web page | Local stick, logger type **Solis stick with only a status web page** | Only what the page shows, refreshed by the stick about every five minutes. |
| Solis S5-WiFi-ST | Local stick, logger type **Solis S5-WiFi-ST (experimental)**, or SolisCloud | Solis does not officially support local access, and newer firmware closes port 502. Add SolisCloud as backup. |
| RS485 gateway (Waveshare, Elfin EW11, USR) wired to the inverter | Local stick, logger type **RS485 gateway** | Modbus TCP mode or transparent mode, to match the gateway. |
| S4-WIFI-ST, S6 USB, 4G/GPRS sticks, or you are not sure | SolisCloud | No local port is known for these sticks. If you find one open, please [open an issue](https://github.com/taurgis/ha-solis/issues). |

To find your stick model, read the label on the stick, or open the plant's
data logger in the SolisCloud app and check the logger details.

## Pages

- [Installation](installation.md)
- [Configuration](configuration.md)
- [Entities](entities.md)
- [Data updates](data-update.md)
- [Use cases](use-cases.md)
- [Examples](examples.md)
- [Limitations](limitations.md)
- [Troubleshooting](troubleshooting.md)
- [Removal](removal.md)
- [Lab screenshots](screenshots.md)

Research notes:

- [Solis API and local access](research/solis-api.md)
- [Dongles and gateways](research/dongles.md)
- [Solarman local protocol](research/solarman-local.md)
- [S2-WL-ST local reads](research/s2-wl-st.md)
- [Solis web page stick](research/web-page-stick.md)
