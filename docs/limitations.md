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
- `device_class: energy_storage` is not used. The 2024 developer portal lists
  `batteryCapacity` and `inverterBatteryCapacity` as "battery capacity" with
  no unit. The V2.0.2 samples do not include those fields or a companion
  unit string. `batteryCapacitySoc` is a percentage. Plant `capacity` is
  nameplate kW. SoC times an assumed kWh figure would invent stored energy.
  See the research note.
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
- Local reads are Solarman V5 on TCP 8899, or Modbus TCP on port 502 for a
  Solis S2-WL-ST. A Wi-Fi S3 stick has no local port; use cloud only for
  those. See [the Solarman note](research/solarman-local.md) and
  [the S2-WL-ST note](research/s2-wl-st.md). Some S2 firmware closes port 502
  or allows only one of SolisCloud and local Modbus.
- The stick is off whenever the inverter is off. Night has no TCP and no
  discovery. The integration keeps energy totals, publishes live power as 0,
  and marks the data source `asleep`. It does not open an outage repair for
  that.
- Registers 3060-3063 (inverter serial) did not answer on the stick this map
  was checked against. When that read is silent, the device serial is the
  one learned at setup, or `L` plus the logger serial. That can differ from
  the SolisCloud inverter serial, so a local-only entity id will not match
  an older cloud entity for the same inverter.
- IGEN does not publish a runnable LSW-3 or LSE-3 image. The lab emulator
  follows the protocol and frames captured from a real stick.
