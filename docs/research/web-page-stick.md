# Solis stick with only a status web page

Input note, not a spec node. Read from a real stick on 2025: collector
firmware `10010106`, web version `2.2.0`, inverter model code `E4`
(Solis-mini, firmware `0F000F`), SolisCloud collector serial
`7A122C096F409F31`, Wi-Fi module on Espressif OUI `B8:D6:1A`.

## What it exposes

- Port 80 only. Ports 502, 8899, 30003, 4196, 8080 and 443 refuse the
  connection. UDP 48899 (`WIFIKIT-214028-READ`, `HF-A11ASSISTHREAD`) gets no
  reply, so it is neither a Solarman stick nor an S2-WL-ST.
- lwIP/1.3.1 HTTP/1.0. `401` with `WWW-authenticate: Basic realm="solis"`
  without credentials. User `admin`, password `123456789` on this firmware.
  Other S3 firmware replaces the password with the Wi-Fi password after the
  stick joins the network ([hn/ginlong-solis](https://github.com/hn/ginlong-solis)).
- The menu has Status, Wireless, Cable, Restart and Reset. `lang.js` carries
  labels for Working mode (`select.html`) and Remote server (`remote.html`),
  and the pages that would set a server B. This firmware does not serve
  them: the connection is closed with no reply. `wan.html` and
  `wirepoint.html` exist. Nothing in the menu enables Modbus. Solis lists only
  the S2-WL-ST for Modbus TCP ([developer page](https://developer.soliscloud.com/guide/modbus.html));
  the S3-WIFI-ST is cloud only in every integration surveyed.

## The data pages

Both pages return a semicolon string. The reply is padded: NUL bytes in front
(33 of them on this stick) and spaces behind, so fields are stripped of both.

`/inverter.cgi`: serial; firmware (the same string SolisCloud shows as the
inverter version, here `0F000F`); model code (`E4`); temperature in degrees;
AC power in watts; energy today in kWh (six decimals, 0.1 resolution); energy
total in kWh (whole numbers); alarm word (`NO`).

`/moniter.cgi` (spelling is the stick's): field 2 collector serial, 3 stick
firmware, 4 to 6 access point status, ssid and ip, 7 to 11 station status,
ssid, rssi, ip and MAC, 12 and 13 remote server A and B (`Connected`,
`null`).

Another firmware prints `;860041;205;20.1;790;0.300000;d;NO;`, an empty serial
and decimal fields ([HA community post](https://community.home-assistant.io/t/using-the-restful-integration-to-get-data-from-a-solis-inverter-with-a-wifi-stick/891939)).
With the field order of [hn/ginlong-solis](https://github.com/hn/ginlong-solis)
(serial, firmware, model, temperature, power, today, total, alarms) the total
there is `d`. The parser therefore requires only a plausible power field;
temperature and energy that are not plausible numbers are left empty, and the
alarm word is optional.

## The IGEN-made status page

Sticks made by IGEN/Solarman for Solis (and for other brands) serve no
`inverter.cgi`. Their `/status.html`, behind Basic auth (`admin`/`admin` by
default), carries the readings as JavaScript variables
([gist](https://gist.github.com/s12wu/12d01bdaafc051d7e3ae83c3fbaec1b8),
[solis2mqtt](https://github.com/thijsmans/solis2mqtt),
[evcc `solarman` template](https://github.com/evcc-io/evcc/blob/master/templates/definition/meter/solarman.yaml),
[HA community](https://community.home-assistant.io/t/getting-data-from-a-solis-ginlong-inverter/461672)):
`webdata_sn` (inverter serial, space padded), `webdata_msvn`, `webdata_now_p`
(W), `webdata_today_e` and `webdata_total_e` (kWh), `webdata_alarm` (empty
without an alarm), `cover_mid` (stick serial), `cover_ver` (stick firmware)
and `cover_sta_mac`. The visible `<div id="webdata_now_p">` shows `---` until
the script fills it, so only the `var` lines are read. With no inverter data
the values are empty (solis2mqtt then treats power as 0). The integration
reads `inverter.cgi` first and falls back to `status.html` on a 404 or a reply
in another format, then keeps the layout that answered. An empty or `---`
power is reported as zero with the inverter offline, keeping the last totals.

The Basic realm differs between sticks, so auto-detection accepts any Basic
challenge on port 80 and confirms the stick by parsing the data page; only the
`solis` realm is trusted enough to report a wrong password during
auto-detection.

## Refresh cycle

Measured on this stick by one request every 15 seconds for 12 minutes: the
values changed twice, 5 min 02 s apart, and were identical in between
(power 382, 1792 then 1175 W). The Home Assistant REST thread and
[Alex Dekker's Zabbix note](https://alexdekker.uk/ALEX/2022/12/getting-data-from-a-ginlong-solis-inverter-into-zabbix/)
give the same five-minute cycle, independent of how often the page is read.
SolisCloud has the same delay. Power is the instantaneous reading at the
stick's poll, not an average.

## Consequences for the integration

- Kind `solis_web`, port 80, basic auth, one GET of `/inverter.cgi` (or
  `/status.html`) per poll. `/moniter.cgi` (or the `cover_*` variables) is
  read once at setup for the stick serial and MAC.
- A rejected password raises `SolisLoggerAuthError`: it does not fix itself,
  since some firmware switches the web password to the Wi-Fi password.
- Reading faster than the stick refreshes gains nothing. Polling every
  minute keeps the lag after a refresh under a minute.
- The page lacks month and year energy, frequency, phases, strings and the
  series name. In `both` mode SolisCloud supplies them, at most every five
  minutes.
