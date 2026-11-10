# Limitations

- SolisCloud refreshes inverter samples about every five minutes. Polling
  faster does not produce newer cloud data.
- The published limit is 2 calls per second per endpoint. This client defaults
  to one call every two seconds per endpoint because faster traffic is a
  common cause of HTTP 502.
- The plant timezone from the API is a fixed numeric offset, not an IANA
  zone. By default, Home Assistant's timezone is used for "today" when it is
  within an hour of that offset, so daylight saving is followed for a plant
  in the same region. A plant several hours away keeps the Solis offset.
  Set the plant timezone option to an IANA name or to `plant` to override
  that. The integration cannot invent energy the cloud has already moved
  into the next day; it holds the previous reading until the boundary it was
  configured to use.
- Lifetime grid import and export are not in the polled endpoints. Daily
  plant sensors are what the energy dashboard can use for those.
- Control writes (`/v2/api/control`, `/v2/api/atRead`) are not implemented.
  Polling them has been reported to drop the logger's cloud session.
- `device_class: energy_storage` is not used. SolisCloud reports battery
  state of charge and plant capacity in kW, not stored energy in kWh.
  Deriving kWh from state of charge would invent a number.
- Plants stay in one config entry. Config subentries would split that entry
  and put the existing entity ids at risk. The plant picker already selects
  any number of plants under one API key.
- No purpose-specific trigger or condition platform is registered. Battery
  and energy device classes already supply Home Assistant's built-in triggers
  where the core version has them.
- The integration does not add its own derived energy sensors. From Home
  Assistant 2026.11 the energy dashboard can derive those from the existing
  power sensors.
- The legacy Ginlong `/cpro/` portal API is not implemented.
- S3-WIFI-ST sticks do not expose a local Modbus port. Local reads are a
  possible later transport, not part of this version. See
  [the research note](research/solis-api.md).
