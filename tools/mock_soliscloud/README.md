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
inverter, across two plants. This process only speaks the cloud HTTP API.

`python -m mock_soliscloud.stick` is the logger emulator. The default is
Solarman V5 on TCP 8899 and UDP 48899. `--kind s2` is an S2-WL-ST: Modbus
TCP on port 502, no discovery broadcast, function 3 and function 4.
`POST /__control` accepts `night`, `port_closed`, `silent`, `max_clients`
and `close_disables_port` for the failure modes documented in
`docs/research/s2-wl-st.md`.
