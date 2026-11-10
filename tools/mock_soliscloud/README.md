# Mock SolisCloud

HTTP stand-in for SolisCloud Platform API v2. It checks the HMAC-SHA1
`Content-MD5` signature the integration sends.

```bash
PYTHONPATH=.:tools .venv/bin/python -m mock_soliscloud --port 8765
```

`POST /__control` with `{"mode": "server_error"}` (also `auth_error`,
`clock`, `rate_limit`, `timeout`, `abnormal`) is a test hook. `GET /__expected`
returns the watts and kilowatt-hours the integration should show.

The fixture catalog covers a single-phase grid inverter, a three-phase hybrid
with a battery, a second three-phase inverter that is offline, and a stale
inverter, across two plants. A future Modbus or Solarman V5 emulator should
produce the same snapshot fields. It should be a separate server. This process
only speaks the cloud HTTP API.
