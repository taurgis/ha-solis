# Solis

Home Assistant integration for SolisCloud plants and inverters.

This is a rewrite of [hultenvp/solis-sensor](https://github.com/hultenvp/solis-sensor)
(Apache-2.0). It is not a GitHub fork. See [NOTICE](NOTICE) and
[docs/research/solis-api.md](docs/research/solis-api.md).

The domain stays `solis`, so replacing the upstream custom component can keep
existing entities. Uninstall that copy first. The two cannot be installed in
the same `custom_components/solis` folder.

Reads use the SolisCloud API key and secret. Inverter control writes are not
sent. The cloud publishes samples about every five minutes, and the client
stays under the platform rate limit.

## Supported devices

Any inverter on a SolisCloud account: grid and hybrid, single phase and three
phase, with or without a battery. The plant, the data logger, and an export
power manager are devices when the account has them. The legacy Ginlong
portal API is not supported.

## Use cases

- Energy dashboard solar production from inverter **Energy total**, and
  battery charge and discharge from the hybrid lifetime totals.
- A notification when the alarm binary sensor turns on.
- Automations that run only while the inverter is online and producing.
  `0` is a fresh offline sample. `unavailable` is a stale sample or a failed
  poll.

See [docs/use-cases.md](docs/use-cases.md) and [docs/examples.md](docs/examples.md).

## Install

Requires Home Assistant 2025.10.0 or newer.

1. In SolisCloud, open Account → API management and create a key. Solis
   sometimes enables that page only after a support ticket. Copy the key id
   and the secret. A portal username is not used.
2. Install with [HACS](https://hacs.xyz/) as a custom repository
   (`https://github.com/taurgis/ha-solis`), or copy `custom_components/solis`
   into the config directory.
3. Restart Home Assistant.
4. Add the Solis integration. The international API URL is
   `https://www.soliscloud.com:13333`. An account created against
   `https://v3.soliscloud.com:13333` should keep that host.
5. Select the plants to follow.

Public `http://` URLs are stored as `https://`. Loopback, private addresses
and single-label hosts such as the lab's `mock` may stay on `http`.

## Migrating from hultenvp/solis-sensor

Remove the upstream custom component, install this one, and restart. Config
entries from solis-sensor 4.x are migrated on startup. Entity unique ids of
the form `{serial}{config name} {sensor label}` are rewritten to
`{serial}_{key}` when they are still in the registry, including phase and PV
string sensors. Plant import, export and load move onto the plant device and
keep the old entity id. If a new entity was already created beside the legacy
one, the legacy entity id is kept and the duplicate is removed. Migration
runs again when an inverter that was missing at startup appears later.

The control API is not migrated. A repair explains that. Battery schedules
already stored on the inverter are left as they are.

## Remove

Delete the Solis entry under Settings → Devices & services. Entity history
for that entry is removed with it. The API key on SolisCloud is unchanged.
Revoke the key there if this Home Assistant instance should stop reading the
account.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). The mock SolisCloud server checks the
real HMAC signature:

```bash
PYTHONPATH=. .venv/bin/python -m mock_soliscloud --port 8765
```
