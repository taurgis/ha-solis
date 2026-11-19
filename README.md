# Solis

Home Assistant integration for Solis inverters.

- Reads your inverter, battery and plant from the SolisCloud API, from the
  data logger stick on your home network, or from the stick with SolisCloud
  as a backup.
- Read-only. It never changes inverter settings or battery schedules.
- Energy dashboard ready: lifetime production, battery, grid import and
  export, and house load are `total_increasing` energy sensors.

It works with Solis inverters read through SolisCloud or a supported local
stick: grid and hybrid, single phase and three phase, with or without a
battery. The plant, the data logger and an export power manager are
devices too when the account has them. Inverter control and the legacy
Ginlong portal API are not supported.

This is a rewrite of [hultenvp/solis-sensor](https://github.com/hultenvp/solis-sensor)
(Apache-2.0). It is not a GitHub fork. See [NOTICE](NOTICE). It uses the same
`solis` domain, so it can replace that integration and keep your entities.
Uninstall the old copy first: both cannot sit in the same
`custom_components/solis` folder.

This project is not affiliated with or endorsed by Ginlong (Ningbo)
Technologies or Solis. "Solis" and "SolisCloud" are their names, used here
only to say what the integration talks to.

## Which connection should I pick?

When you add the integration, the first question is how Home Assistant reads
the inverter. Find your stick below.

| Your stick | Pick | Notes |
| --- | --- | --- |
| Solarman / IGEN stick: DLS-W, DLS-L, LSW-3, LSE-3 | Local stick (or Local stick with SolisCloud backup) | Found automatically on your network while the inverter is producing. |
| Solis S2-WL-ST (Wi-Fi + LAN) | Local stick, after Modbus TCP is enabled on the stick firmware | Solis's own guide says enabling Modbus TCP disconnects the stick from SolisCloud. Some firmware builds are reported to allow both. If you want to keep SolisCloud, check [the S2-WL-ST notes](docs/research/s2-wl-st.md) first. |
| Solis S3-class Wi-Fi stick whose only local service is a status web page | Local stick, logger type **Solis stick with only a status web page** | Only what the page shows, refreshed by the stick about every five minutes. Add SolisCloud as backup for the rest. |
| Solis S5-WiFi-ST | Local stick, logger type **Solis S5-WiFi-ST (experimental)**, or SolisCloud | Solis does not officially support local access, and newer firmware closes port 502. Add SolisCloud as backup. |
| RS485 gateway (Waveshare, Elfin EW11, USR) wired to the inverter | Local stick, logger type **RS485 gateway** | Pick Modbus TCP mode or transparent mode to match the gateway setting. |
| S4-WIFI-ST, S6 USB, 4G/GPRS sticks, or you are not sure | SolisCloud | No local port is known for these sticks. SolisCloud works with every stick. If you find one open, please [open an issue](https://github.com/taurgis/ha-solis/issues). |

**Local stick with SolisCloud backup** reads the stick first and uses
SolisCloud when the stick is busy and for values the stick does not have.

### How do I find my stick model?

- Look at the label on the stick plugged into the inverter. The model
  (for example `DLS-L` or `S2-WL-ST`) and its serial are printed there.
- Or open the SolisCloud app, go to the plant, open the data logger, and
  check the logger details. They show the model, the serial and the
  logger's IP address on your network.

More detail on each stick is in [Supported loggers](docs/research/dongles.md).

## Install

Requires Home Assistant 2025.10.0 or newer.

1. Install with [HACS](https://hacs.xyz/) as a custom repository
   (`https://github.com/taurgis/ha-solis`, category Integration), or copy
   `custom_components/solis` into your config directory.
2. Restart Home Assistant.
3. Go to Settings → Devices & services → Add integration → Solis, and pick a
   connection.

For SolisCloud you need an API key: in SolisCloud open Account → Basic
settings → API management and create one. Solis sometimes enables that page
only after a support ticket. The key ID and secret are all the integration
needs; your portal username and password are not used.

For a local stick, set it up during the day. The stick is powered by the
inverter and switches off with it at night. Reserve the stick's IP address
in your router so it does not change.

Full steps: [Installation](docs/installation.md) and
[Configuration](docs/configuration.md).

## What you get

Each plant, inverter, data logger and export power manager is a device.
Inverters have power, energy, temperature, frequency, phase and PV string
sensors, plus battery sensors on hybrids. See [Entities](docs/entities.md).

Lifetime counters for the Energy dashboard:

| Dashboard slot | Sensor |
| --- | --- |
| Solar production | Inverter **Energy total** |
| Grid consumption | Inverter **Grid import total** |
| Return to grid | Inverter **Grid export total** |
| Battery in / out | **Battery charged total** / **Battery discharged total** |

**Load total** is the house consumption counter. The dashboard works out
home usage itself, so it has no slot for it, but it is handy on cards and
in statistics.

Grid import total, grid export total and load total come from SolisCloud.
They are added once the inverter reports them, so a local-only entry and a
plant without a meter do not have them.

### Sign conventions

- **Grid power** (from SolisCloud `psum`) is positive while exporting and
  negative while importing. Home Assistant's own convention is the other way
  round. When you add a grid power sensor in the Energy dashboard power
  settings, choose the inverted option.
- **Battery power** is positive while charging and negative while
  discharging.

## Migrating from hultenvp/solis-sensor

Remove the old custom component, install this one and restart. Config
entries from solis-sensor 4.x are migrated on startup, and existing entity
ids are kept when the old registry rows are still there. That includes
phase and PV string sensors, and the lifetime counters:

| solis-sensor name | New sensor |
| --- | --- |
| Total Energy Purchased | Grid import total |
| Total On-grid Energy | Grid export total |
| Total Energy Used (or Total Consumption Energy) | Load total |

Daily plant import, export and load move onto the plant device and keep
their old entity id. Migration runs again when an inverter that was missing
at startup appears later.

The control API is not migrated. A repair explains that. Battery schedules
already stored on the inverter are left as they are.

## More

- [Use cases](docs/use-cases.md) and [example automations](docs/examples.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Limitations](docs/limitations.md)
- [Removal](docs/removal.md): delete the entry under Settings → Devices &
  services. Your SolisCloud API key stays valid until you revoke it in
  SolisCloud.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). Research notes on the SolisCloud API
and the local protocols are under [docs/research](docs/research/solis-api.md).
