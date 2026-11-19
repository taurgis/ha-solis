# Limitations

- SolisCloud refreshes inverter samples about every five minutes. Polling
  faster does not produce newer cloud data.
- The published limit is 2 calls per second per endpoint. This client defaults
  to one call every two seconds per endpoint because faster traffic is a
  common cause of HTTP 502.
- SolisCloud gives the plant timezone as a numeric offset, and sometimes
  also as a zone name. **Automatic** uses the plant's zone name when it
  matches the offset, otherwise Home Assistant's zone when it is within an
  hour of the offset, so daylight saving is followed. A plant several hours
  away keeps the Solis offset. Pick a zone or **Plant offset from Solis** in
  the options to override that. The integration cannot invent energy the
  cloud has already moved into the next day; it holds the previous reading
  until the boundary it was configured to use.
- Lifetime grid import, grid export and load totals come from SolisCloud
  only. A local-only entry, or a plant without a meter, does not have them.
  Grid power is not read locally from a hybrid either: the register there
  is the inverter's AC port, not the meter.
- Control writes (`/v2/api/control`, `/v2/api/atRead`) are not implemented.
  Polling them has been reported to drop the logger's cloud session.
- `device_class: energy_storage` is not used. The 2024 developer portal lists
  `batteryCapacity` and `inverterBatteryCapacity` as "battery capacity" with
  no unit. The V2.0.2 samples do not include those fields or a companion
  unit string. `batteryCapacitySoc` is a percentage. Plant `capacity` is
  nameplate kW. SoC times an assumed kWh figure would invent stored energy.
  See the research note.
- Two **Local stick with SolisCloud backup** entries may share one
  SolisCloud account, for example two sticks on one plant. The entry that
  added the plant first keeps the plant and export power manager devices;
  the other entry shows its own inverter and logger only. When the first
  entry is removed, the other one takes the plant over on its next poll.
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
- Local reads are Solarman V5 on TCP 8899, Modbus TCP on port 502 for a
  Solis S2-WL-ST or an S5-WiFi-ST whose firmware still listens, Modbus TCP
  to an RS485 gateway, or experimental Modbus RTU over TCP for a
  transparent tunnel. A stick that only has a status web page is read from
  that page (`inverter.cgi` or `status.html`: power, temperature, energy
  today and total, firmware, alarm, refreshed by the stick about every five
  minutes); SolisCloud fills the rest.
  No local listener is known on S4-WIFI-ST, S6 USB sticks, other S3
  firmware or 4G/GPRS sticks (only the S3-WIFI-ST is confirmed by an
  independent source); use cloud, or a gateway on the inverter
  RS485 port. See [dongles](research/dongles.md),
  [the Solarman note](research/solarman-local.md) and
  [the S2-WL-ST note](research/s2-wl-st.md). Some S2 and S5 firmware closes
  port 502 or allows only one of SolisCloud and local Modbus.
- A stick powered by the inverter is off whenever the inverter is off.
  Night has no TCP and no discovery. The integration keeps energy totals,
  publishes live power as 0, and marks the data source `asleep`. A gateway
  that refuses TCP is not night: the last power is held and, after 10
  minutes, a repair opens. A gateway that accepts TCP and gets no Modbus
  answer is the inverter being off, and live power goes to 0. A gateway
  powered from the inverter USB port looks like a down gateway at night.
- Only Solarman / IGEN sticks are discovered automatically. An S2-WL-ST, an
  S5, a web-page stick and RS485 gateways need their address typed, and
  should get a DHCP reservation.
- Registers 3060-3063 (inverter serial) did not answer on the stick the
  3000-range map was checked against. When that read is silent, the device serial is the
  one learned at setup, or `L` plus the logger serial. That can differ from
  the SolisCloud inverter serial, so a local-only entity id will not match
  an older cloud entity for the same inverter.
- IGEN does not publish a runnable LSW-3 or LSE-3 image. The lab emulator
  follows the protocol and frames captured from a real stick.
