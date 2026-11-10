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
.venv/bin/python -m coverage report \
  --include='custom_components/solis/config_flow.py' --fail-under=100
```

`pre-commit` runs ruff. CI runs ruff, mypy, the 95% suite and the config-flow
coverage gate.

The mock SolisCloud server checks the real HMAC signature:

```bash
PYTHONPATH=. .venv/bin/python -m mock_soliscloud --port 8765
```

`lab/docker-compose.yml` boots Home Assistant next to that mock. Add the
integration with API URL `http://mock:8765`, key id `test-key` and secret
`test-secret`.

`scripts/check_minimum_home_assistant.py` boots the minimum Core version from
`hacs.json` and checks entity values, a 502 and recovery, a null and a zero
lifetime total, and the auth repair. Pass a version argument to boot another
tag, such as the current stable release.

```bash
.venv/bin/python scripts/check_minimum_home_assistant.py
.venv/bin/python scripts/check_minimum_home_assistant.py 2026.11.1
```

Changes that affect users get a changeset under `.changeset/`. Do not tag or
publish a release from a pull request.
