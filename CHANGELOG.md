# Changelog

## 0.7.0

### Minor Changes

- 9a0eb3e: Live readings follow Home Assistant's availability rules while the inverter is off. What the inverter produces (AC output, PV string power and current, battery power and current, backup and smart-load power) reads 0 whenever SolisCloud reports it offline, even when its last sample is hours old, instead of going unavailable overnight. What it only measures (grid power, house consumption, the grid-meter phase power, apparent and reactive power, grid voltage and frequency, PV and battery voltage, temperature) reads unknown instead of 0, since those keep changing without the inverter. A failed or stale read still makes them unavailable, and energy totals are unchanged. The battery charging binary sensor reads off while the inverter is offline.

## 0.6.0

### Minor Changes

- 2119e5f: Moving between SolisCloud and a local stick, in either direction, and replacing a stick keep your entities and their history. A poll of the stick no longer deletes the plant, export meter or other inverter devices that only SolisCloud reports, so a migrated solis-sensor entry can move to a stick (Reconfigure → Local stick, then Connection → Local stick only) without losing plant history. A poll interval still on the default of the connection you leave now follows the new one (60 seconds for a stick, 300 seconds for SolisCloud only), and swapping a web-page stick for a Modbus stick deletes the saved web password. On an entry with a stick and SolisCloud backup, the plant now keeps updating from SolisCloud while the stick is unavailable.

## 0.5.0

### Minor Changes

- a570020: New SolisCloud sensors that solis-sensor had: grid import and export this month and this year, battery charged and discharged this month and this year, backup (EPS) load power and today's backup energy, smart-load port power and today's energy (disabled by default), and per-phase meter voltage, current, apparent power and reactive power (disabled by default). Each is added once the inverter reports the value, and solis-sensor's entities for these readings keep their entity IDs and history when you migrate.

## 0.4.0

### Minor Changes

- 2ea03df: Third audit. Reauthentication accepts a rotated SolisCloud key: a new key ID is accepted when it still sees every plant the entry reads, and a SolisCloud-only entry's unique ID follows the key ID on reauth and reconfigure. Switching an entry's connection no longer lets two entries read the same account. A Modbus or Solarman stick with SolisCloud backup no longer creates and then deletes the cloud plant, logger and EPM devices each time the stick pauses. A logger repair opened before a reload now clears when the stick sleeps. A Modbus TCP session (S2-WL-ST, S5, gateways) stays open after one timeout and skips the late reply, because closing the socket can wedge port 502 on S2-WL-ST firmware. Solarman V5 requests use an 8-bit sequence and skip frames that are not inverter data, like pysolarmanv5. SolisCloud code 1 "Communication error" is retried and the last sample held, instead of failing the update; a code 429 in the body is a rate limit. A pending SolisCloud alarm whose end time has passed no longer keeps the Alarm sensor on. A bracketed `(UTC+10:00)` plant time zone is parsed. "Phase n power" is renamed "Grid phase n power", because SolisCloud's `pA`/`pB`/`pC` are the grid meter's phase power; entity IDs are kept. Device removal uses the 2026.8 device registry API. Adding a stick with backup on an account that already has a SolisCloud-only entry says to use Reconfigure on that entry. Data source has state icons, and several texts and docs were corrected, including the gateway port list, the timezone help, two Dutch strings, and the S5-WiFi-ST row in the connection guide. Two local-with-backup entries on one SolisCloud account no longer collide: the entry that added a plant first keeps the plant and EPM sensors, each web-page stick entry shows only its own logger, and the other entry takes the plant over when the first is removed. On Modbus TCP and RTU links, a hybrid inverter that also answers the 3000-range registers is detected as a hybrid from register 35000 (Solarman V5 sticks are not asked, because a high read locks the Solis-mini 4G). The docs no longer claim for certain that S4-WIFI-ST, S6 USB and 4G sticks have no local port.

## 0.3.0

### Minor Changes

- f905b0d: Read more Solis-compatible loggers on the LAN: Modbus TCP gateways such as Waveshare, Elfin and USR, an experimental S5-WiFi-ST while port 502 is open, and experimental Modbus RTU over TCP for a transparent serial tunnel. Auto-detect is unchanged. Sticks with no local port stay on SolisCloud.
- 585d4b4: Setup now starts with a simple choice: SolisCloud only, Local stick only, or Local stick with SolisCloud backup. The local stick form shows only the logger type, address and inverter serial; port, slave, logger serial and gateway model moved to a collapsed Advanced section, and the web password is asked only when the stick rejects the factory one. Reconfigure is a menu (SolisCloud account, Plants, Local stick, Connection), and a local entry can add SolisCloud as a backup there. The options dialog has a timezone dropdown (Automatic, Plant offset from Solis, or a typed zone) with the request limits under Advanced, and saving reloads the entry. New Solarman sticks are discovered over DHCP (MAC prefixes E8:FD:F8 and BC:54:F9) and offered with a confirm step. Web page sticks that serve `status.html` (made by IGEN/Solarman for Solis) are supported, and a rejected stick password now asks for the new one through reauthentication. New sensors: Grid import total, Grid export total and Load total from SolisCloud; they take over the history of solis-sensor's Total Energy Purchased, Total On-grid Energy and Total Energy Used. Hybrid inverters read locally now report AC power, temperature, frequency and status. Local entries no longer create an empty "Local" plant device, the logger device carries the stick's MAC, phase sensors of single-phase inverters are disabled by default, lifetime energy shows one decimal, and battery power and the alarm have direction and state icons. A rejected API key, a SolisCloud outage and abnormal cloud data no longer open repairs (reauthentication or a log line instead), no repair is fixable any more, several repairs link to the troubleshooting page, and repairs stay open across a reload. The documentation was rewritten with a connection guide per stick model, the grid and battery sign conventions for the Energy dashboard, and new example automations. The upscaled @2x brand images were removed.
- 3d2f1b8: Read a Solis stick that has no Modbus or Solarman port but a status web page (some S3-class firmware). The new Solis web page logger kind reads power, temperature, energy today and total, firmware and alarm from the stick, which refreshes them about every five minutes like SolisCloud. Auto-detect offers it when ports 502 and 8899 are closed and port 80 asks for the Solis login. With SolisCloud also configured, the web page wins and the cloud fills month and year energy, frequency, phases, strings and the series name, read at most every five minutes. A web password the stick keeps rejecting (three times in a row, or for 10 minutes) asks for the new one with a Reauthenticate card. An empty password field tries the factory passwords `123456789` and `admin` (IGEN-made sticks).

### Patch Changes

- 6663d36: Hold the last local sample when a logger is offline after a restart, reject short or mismatched Modbus frames, and keep one socket per stick. Cloud polling cannot be set faster than every 60 seconds.
- 2f20f96: Quality audit fixes. A paused or offline local logger no longer triggers a full SolisCloud read on every poll, and the cloud read behind a local stick has a 20 second budget. A rejected API key or wrong clock is now reported (repair and reauth) for local-plus-cloud entries, and a rejected key stops polling through Home Assistant's reauth. Today, month and year energy no longer stay pinned to yesterday's number after a day or month rollover. Local entries use Home Assistant's timezone for the day boundary, and a night snapshot clears voltages and temperature. Web stick pages with NaN, infinite or out-of-range values are rejected, and a refused port is a fault, not night. Modbus TCP replies that are not a valid response, and discovery replies without an IP address, are handled. Register values are rounded. The snapshot file is written at most every five minutes and removed with the entry. The options dialog, reconfigure and reauth for entries with a stick, and several translations are corrected. The API secret field is masked.
- 2732bb2: Second quality audit. A discovery reply must come from the address it announces and be on the local network, and DHCP never moves a web-page stick (which holds a password) or accepts a public address. A peer that streams junk can no longer hold a logger read open. Plain http to a public or numeric host is upgraded to https, and redirects from SolisCloud are not followed. Repairs left by a failed setup clear after recovery, and the last snapshot is saved when Home Assistant stops. A night snapshot also stops the battery current, a grid frequency of 0 is no reading, a local entry no longer creates placeholder plant sensors, and the alarm sensor also reports an alarm state. The Connection field is a translated dropdown, a missing key id and a missing logger address have their own error messages, and troubleshooting, configuration and data-update docs now cover the web stick, the logger repairs and the retry rules. Diagnostics also redact the entry title.

## 0.2.0

### Minor Changes

- af2b66f: Initial SolisCloud integration: config flow, coordinator, unit handling, alarms, and a signature-checking mock.
- 1a4ddda: Read a Solis S2-WL-ST on Modbus TCP port 502, auto-detect it beside a Solarman stick, and explain firmware that closes port 502 or answers TCP without Modbus.

### Patch Changes

- b142410: Hold energy totals across null and zero cloud samples, follow daylight saving when the plant offset is within an hour of Home Assistant, and correct migration, retries and the documented minimum Home Assistant version.
- a3829cb: Sign Solis battery power from the charge and discharge components, so positive means charging and negative means discharging.
- 878a141: Hold the last good SolisCloud snapshot through an outage, hide stale live readings per inverter, and keep energy totals from moving backwards when the cloud sends a reset or a spike.
- b957172: Ship the published Solis icon and logo in the integration brand folder so HACS and Home Assistant 2026.3 show them without the brands CDN.
- ccbbaf7: Score the Solis integration against the Home Assistant quality scale, translate update failures, and require every module to stay above 95 percent coverage.
- b944b38: Read a Solarman logger on the LAN, fall back to Modbus TCP when V5 is not the framing, and pause local reads when the stick resets instead of treating that as night.

## 0.1.0

Initial release of the SolisCloud rewrite. See `docs/research/solis-api.md`.
