# Contributing

Development uses Python 3.14, which is what current Home Assistant and the
test harness require. The integration still loads on Home Assistant 2025.10.0,
whose container is Python 3.13, so ruff targets 3.13 and `except` clauses stay
parenthesized.

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/ruff check custom_components tests tools scripts
.venv/bin/ruff format --check custom_components tests tools scripts
.venv/bin/mypy --strict custom_components/solis
.venv/bin/pytest --cov=custom_components/solis --cov-fail-under=95
.venv/bin/python scripts/check_module_coverage.py
.venv/bin/python -m coverage report \
  --include='custom_components/solis/config_flow.py' --fail-under=100
```

`pre-commit` runs ruff.

## CI gates

Every pull request must pass:

- ruff lint and `ruff format --check` on `custom_components`, `tests`,
  `tools` and `scripts`;
- `mypy --strict` on `custom_components/solis`;
- pytest with at least 95% coverage overall, at least 95% for every module
  (`scripts/check_module_coverage.py`), and 100% for `config_flow.py`;
- hassfest and the HACS validation action;
- a boot of the minimum Home Assistant version from `hacs.json` against the
  mock SolisCloud (`scripts/check_minimum_home_assistant.py`).

A pull request without a changeset gets a warning.

## Mocks and labs

Most loggers are not on anyone's desk, so the tests and labs use emulators
that behave like the real hardware, including its faults. Prefer them over
tests that only feed the expected answer in.

- `tools/mock_soliscloud` is a SolisCloud stand-in that checks the real HMAC
  signature, serves a catalog of plants (single-phase grid, three-phase
  hybrid with battery, offline, stale) and has fault modes such as
  `server_error`, `auth_error`, `clock`, `rate_limit`, `timeout` and
  `abnormal` (`POST /__control`).

  ```bash
  PYTHONPATH=.:tools .venv/bin/python -m mock_soliscloud --port 8765
  ```

- `tools/mock_soliscloud/stick.py` emulates a Solarman V5 stick (with UDP
  discovery) or an S2-WL-ST, with night, closed-port, silent and
  one-client modes. `tools/mock_soliscloud/web_stick.py` emulates a stick
  that only serves a status web page, in both the `inverter.cgi` and the
  `status.html` layouts.
- `lab/docker-compose.yml` boots Home Assistant next to the mock cloud. Add
  the integration with API URL `http://mock:8765`, key ID `test-key` and
  secret `test-secret`.
- `tools/lab` runs every logger kind on its own loopback address and boots
  Home Assistant against them. See [tools/lab/README.md](tools/lab/README.md).

  ```bash
  python3.14 tools/lab/run_lab.py 2026.11.1 2025.10.0
  ```

`scripts/check_minimum_home_assistant.py` boots the minimum Core version from
`hacs.json` and checks entity values, a 502 and recovery, a null and a zero
lifetime total, and reauthentication. Pass a version argument to boot another
tag, such as the current stable release.

```bash
.venv/bin/python scripts/check_minimum_home_assistant.py
.venv/bin/python scripts/check_minimum_home_assistant.py 2026.11.1
```

## Changesets and releases

Changes that affect users get a changeset under `.changeset/` (`npm run
changeset`). Do not tag or publish a release from a pull request.

Do not put real serial numbers, e-mail addresses or keys in code, tests or
docs. Use made-up values such as `1031000000000001`.
