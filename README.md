# Solis

Home Assistant integration for SolisCloud plants and inverters.

This is a rewrite of [hultenvp/solis-sensor](https://github.com/hultenvp/solis-sensor)
(Apache-2.0). It is not a GitHub fork. See [NOTICE](NOTICE) and
[docs/research/solis-api.md](docs/research/solis-api.md).

The domain stays `solis`, so replacing the upstream custom component can keep
existing entities. Uninstall that copy first. The two cannot be installed in
the same `custom_components/solis` folder.

Reads use the SolisCloud API key and secret, or a data logger on the LAN
while the inverter is producing. Two sticks are supported. A Solarman /
IGEN stick (DLS-L, LSW) speaks Solarman V5 on TCP 8899, with plain Modbus
TCP as a fallback. A Solis S2-WL-ST (4-pin, Wi-Fi + LAN) speaks Modbus TCP
on port 502. Inverter control writes are not sent. The cloud publishes
samples about every five minutes, and the client stays under the platform
rate limit. A Solarman logger is identified by its serial and MAC, so a new
DHCP lease updates the existing entry. An S2-WL-ST does not answer that
discovery broadcast: reserve its address in the router, and the config entry
keeps the stick serial or the inverter serial when the address changes.

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

## Solis S2-WL-ST

The official 4-pin Wi-Fi + LAN stick replaces a DLS-L. Enable Modbus TCP
before Home Assistant can read it. The integration probes port 502, then
8899, so Auto-detect is enough when you know the IP.

1. Power the inverter so the stick is on. In the SolisCloud app the logger
   address is on the logger details page. On a laptop cabled straight to the
   stick the address is `10.10.100.254` (log in as `admin` and set a password
   on the first visit). That address is not the one Home Assistant uses once
   the stick has joined the house LAN.
2. Ask Solis for stick firmware that keeps port 502 open and still uploads to
   SolisCloud. `100101B4` is the first build with Modbus TCP. `100121cf`,
   `100121d0` and the `100141dx` builds are the ones reported to allow cloud
   and Modbus together. Older firmware disconnects SolisCloud while TCP is
   enabled, and some later updates close port 502 entirely. See
   [docs/research/s2-wl-st.md](docs/research/s2-wl-st.md).
3. In Home Assistant add Solis, choose Local or Local and cloud, and leave
   Logger on Auto-detect. Type the IP. The logger serial is the suffix of the
   setup Wi-Fi name `D_` or `Solis_` (for example `5A12…`), not the inverter
   serial. Slave stays empty unless the inverter is not address 1. Polling
   defaults to 60 seconds and cannot be faster than 30.
4. Reserve that IP in the router's DHCP settings. Do not put a static address
   on the stick unless the SolisCloud LAN settings are the only way you can
   reach it. The stick is not found by the Solarman UDP broadcast, and no
   MAC OUI is published for it.

The same 3000-range register map is used. The owner's Solis-mini-2500-4G
(model E4, slave 1, serial `110E40182280017`) is that map. Registers
3060-3063 hold only eight characters, so type the full inverter serial if
you want the entity ids to match SolisCloud.

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
