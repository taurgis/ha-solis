# Installation

Home Assistant 2026.10.4 or newer.

1. Create an API key in SolisCloud under Account → API management.
2. Install this repository with HACS as a custom repository, or copy
   `custom_components/solis` into `config/custom_components/solis`.
3. Restart Home Assistant.
4. Add the Solis integration. Enter the API URL, key id and secret.
5. Select the plants.

The international API host is `https://www.soliscloud.com:13333`. An account
created against `https://v3.soliscloud.com:13333` should keep that host.

If you already use `hultenvp/solis-sensor`, remove it before installing this
one. Both use the `solis` domain. Config entries from that 4.x integration
migrate, and entity unique ids of the form `{serial}{name}_{label}` are
rewritten to `{serial}_{key}` when they are still in the registry.
