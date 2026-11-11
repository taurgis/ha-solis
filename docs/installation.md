# Installation

Home Assistant 2025.10.0 or newer. That is the same floor used by
`has-marstek-local-api`. The integration uses `runtime_data`, the current
options-flow style and `async_update_reload_and_abort`, which that release
already provides. It does not depend on a 2026-only API.

## API key

1. Sign in to SolisCloud.
2. Open Account → API management.
3. Create a key and copy the key id and the secret.

Solis has required a support ticket before that page is shown. The key id and
secret are the whole read credential. The portal username and password are
not sent.

Use the host that issued the key, including the port:

| Account | URL |
| --- | --- |
| International | `https://www.soliscloud.com:13333` |
| Older keys pinned to v3 | `https://v3.soliscloud.com:13333` |

The config flow offers the international URL. A migrated entry keeps the host
it already had. A public `http://` URL is stored as `https://`.

A Solarman stick on the same LAN can be added on that first step. Discovery
only works while the inverter is producing. Details are in
[Configuration](configuration.md).

## HACS

Add `https://github.com/taurgis/ha-solis` as a custom repository, category
Integration. Download Solis, restart, then Settings → Devices & services →
Add integration → Solis.

## Manual

Copy `custom_components/solis` into `config/custom_components/solis` and
restart.

## Replacing hultenvp/solis-sensor

Both integrations use the domain `solis` and the same folder. Remove the
upstream copy before installing this one. Restart, and the existing config
entry migrates. Unique ids are rewritten when the old registry rows are still
present. See the migration section in the README.

Do not run both at once. A second domain would not keep recorder history.
