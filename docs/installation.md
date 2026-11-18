# Installation

Requires Home Assistant 2025.10.0 or newer.

## HACS

1. In HACS, add `https://github.com/taurgis/ha-solis` as a custom
   repository, category Integration.
2. Download Solis and restart Home Assistant.
3. Go to Settings → Devices & services → Add integration → Solis.

## Manual

Copy `custom_components/solis` into `config/custom_components/solis` and
restart Home Assistant.

## Before you add the integration

### SolisCloud API key

Needed for **SolisCloud only** and **Local stick with SolisCloud backup**.

1. Sign in to SolisCloud.
2. Open Account → Basic settings → API management.
3. Create a key and copy the key ID and the secret. The secret is shown
   only once.

Solis has sometimes required a support ticket before that page is shown.
The key ID and secret are the whole read credential. Your portal username
and password are not used.

Use the host that issued the key, including the port:

| Account | URL |
| --- | --- |
| International | `https://www.soliscloud.com:13333` |
| Older keys pinned to v3 | `https://v3.soliscloud.com:13333` |

The setup form offers the international URL. A migrated entry keeps the
host it already had.

### Local stick

Needed for **Local stick only** and **Local stick with SolisCloud backup**.

- Set it up during the day. The stick is powered by the inverter and is off
  at night, so it cannot be found or tested then.
- Find the stick's IP address in the SolisCloud app (the data logger
  details) or in your router, and reserve it in the router's DHCP settings.
- Solarman / IGEN sticks are found automatically and can also show up under
  Discovered in Settings → Devices & services. Other sticks need their
  address typed.
- An S2-WL-ST needs firmware with Modbus TCP enabled first. See
  [S2-WL-ST local reads](research/s2-wl-st.md).

Then add the integration and pick the connection on the first screen. The
forms are described in [Configuration](configuration.md).

## Replacing hultenvp/solis-sensor

Both integrations use the domain `solis` and the same folder. Remove the
old copy, install this one and restart. The existing config entry migrates,
and entity IDs are kept when the old registry rows are still present. See
the migration section in the [README](https://github.com/taurgis/ha-solis#migrating-from-hultenvpsolis-sensor).

Do not run both at once. Installing this under another domain would not
keep recorder history.
