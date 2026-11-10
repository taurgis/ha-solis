# Limitations

- SolisCloud refreshes inverter samples about every five minutes. Polling
  faster does not produce newer cloud data.
- The published limit is 2 calls per second per endpoint. This client defaults
  to one call every two seconds per endpoint because faster traffic is a
  common cause of HTTP 502.
- The plant timezone is a fixed numeric offset. Daylight-saving transitions
  are not in the API, so "today" counters can roll an hour away from local
  midnight.
- Control writes (`/v2/api/control`, `/v2/api/atRead`) are not implemented.
  Polling them has been reported to drop the logger's cloud session.
- The legacy Ginlong `/cpro/` portal API is not implemented.
- S3-WIFI-ST sticks do not expose a local Modbus port. Local reads are a
  possible later transport, not part of this version. See
  [the research note](research/solis-api.md).
