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

## Install

[HACS](https://hacs.xyz/) as a custom repository, or copy
`custom_components/solis` into your config directory and restart.

Requires Home Assistant 2026.10.4 or newer.

In SolisCloud, open Account → API management and create a key. Add the Solis
integration and paste the API URL (the international default is
`https://www.soliscloud.com:13333`), the key id and the secret. Choose the
plants to follow.

## Remove

Delete the Solis entry under Settings → Devices & services. Entity history
for that entry is removed with it. The API key on SolisCloud is unchanged.

## Development

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest
.venv/bin/ruff check custom_components tests tools scripts
.venv/bin/mypy --strict custom_components/solis
```

The mock SolisCloud server checks the real HMAC signature:

```bash
PYTHONPATH=. .venv/bin/python -m mock_soliscloud --port 8765
```

`lab/docker-compose.yml` boots current stable Home Assistant next to that
mock. `scripts/check_minimum_home_assistant.py` boots the minimum Core version
from `hacs.json` and checks entity values, a 502 and recovery.
