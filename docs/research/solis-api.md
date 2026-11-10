# Solis research

This note is the research behind `taurgis/ha-solis`. The code is a rewrite of
[hultenvp/solis-sensor](https://github.com/hultenvp/solis-sensor) (Apache-2.0,
default branch `master`, latest tag inspected: `v4.0.3`). It is not a GitHub
fork and it is not original Solis work. Copyright headers that existed upstream
are not copied verbatim into new modules; the unit rules, sensor vocabulary
and endpoint list follow that project and the Solis documents below. See
`NOTICE`.

Sources, read for this note:

- SolisCloud Platform API Document V2.0, SolisCloud API 1.2 and SolisCloud API
  1.0.8, as shipped in `hultenvp/solis-sensor` under `doc/`.
- Upstream code at `v4.0.3` and the Ginlong client still present at tag `v3.13.2`.
- Open upstream issues and pull requests as of 2026-11-10 (see the table below).
- Solis developer page [Modbus & Direct Control](https://developer.soliscloud.com/guide/modbus.html).
- Solis service article [S2-WL-ST Modbus TCP](https://solis-service.solisinverters.com/en/support/solutions/articles/44002530087-solis-s2-wl-st-modbus-tcp-communication).
- [fboundy/ha_solis_overview](https://github.com/fboundy/ha_solis_overview),
  [fboundy/ha_solis_modbus](https://github.com/fboundy/ha_solis_modbus),
  [Pho3niX90/solis_modbus](https://github.com/Pho3niX90/solis_modbus) and its
  docs on datalogger firmware closing port 502,
  [StephanJoubert/home_assistant_solarman](https://github.com/StephanJoubert/home_assistant_solarman)
  issue 63 and `solis_hybrid.yaml`,
  [davidrapan/ha-solarman](https://github.com/davidrapan/ha-solarman),
  [jmccrohan/pysolarmanv5](https://github.com/jmccrohan/pysolarmanv5).
- Ginlong/Solis hybrid RS485 map republished by Brian Coghlan
  (`RS485_MODBUS-Hybrid-BACoghlan-201811228-1854.pdf`). Solis later asked for
  the original English manual to be taken down; treat register maps as
  device-specific and check them against the inverter series before writing.

## Domain

The integration domain stays `solis`.

Upstream unique ids were `{serial}{config name} {sensor label}` with spaces
replaced by underscores, and device identifiers were
`{serial}_SolisCloud`. Renaming the entry orphaned history. This rewrite keeps
the domain so a user who removes `hultenvp/solis-sensor` and installs this
custom component still owns the same config-entry domain. On setup we rewrite
those ids to `{serial}_{key}` and `inverter:{serial}` when the old row is
still in the entity or device registry. Config entries from solis-sensor 4.x
(`portal_domain`, `portal_key_id`, `portal_secret`, `portal_plant_id`,
`refresh_ok`, `request_timeout`) migrate to version 2. The portal password is
dropped on purpose.

A second domain would let this sit next to upstream in one Home Assistant.
That cannot preserve recorder history, because history follows the entity id
under the `solis` domain. Side-by-side evaluation needs a second Home Assistant
instance, or uninstalling one of the two. That trade-off is called out for
Thomas at the end.

## SolisCloud Platform API v2

The platform document (V2.0) states:

- TLS only. The documented host is `https://www.soliscloud.com:13333/`.
- Every call is `POST` with `Content-Type: application/json`.
- Every call sends `Content-MD5`, `Content-Type`, `Date` and `Authorization`.
- Responses are JSON `{success, code, msg, data}`. `code` `"0"` is success.
  Upstream and the samples sometimes use a string and sometimes imply an
  integer; compare as a string.
- "All interface returned data (power, energy, frequency, etc.) must be used
  in conjunction with the unit."
- "The update frequency for all interface data is 5 minutes."
- Each listed interface is capped at **2 times/second**.

`hultenvp/solis-sensor` 4.x defaults the host to
`https://v3.soliscloud.com:13333`. The PDF examples also show
`test.soliscloud.com:3333` (port 3333, not 13333). Accounts are pinned to the
host that issued the key. The config flow asks for the URL and offers the
international default. A migrated entry keeps whatever host it already had.

Keys come from SolisCloud → Account → Basic settings → API management. Solis
has also required a support ticket before that page is enabled. The key id and
secret are the whole read credential. A portal username is not required for
`/v1/api/*`. That is upstream issue 517: the old config flow still demanded a
username.

### Signing

```
Content-MD5 = Base64(MD5(raw body bytes))
Date        = GMT, English, "Fri, 26 Jul 2019 06:00:46 GMT"
              The PDF's Java sample uses "EEE, d MMM yyyy HH:mm:ss 'GMT'"
              (day not zero-padded). The known-good solis-sensor client and
              this one zero-pad the day. The server checks the signed string,
              not a second parse of the day number.
string      = POST + "\n" + Content-MD5 + "\n" + Content-Type + "\n"
              + Date + "\n" + CanonicalizedResource
signature   = Base64(HMAC-SHA1(secret, string))
Authorization = "API " + keyId + ":" + signature
```

`CanonicalizedResource` is the path only, for example `/v1/api/inverterDetail`.

The PDF writes `Authorization:"API"+apiId+":"+Sign` with no space, and the
worked example in section 2.4 uses `Content-Type: application/json` while
section 2.2 and later captures use `application/json;charset=UTF-8`. The
signature covers the Content-Type byte-for-byte. This client sends
`application/json` and `API {key}:{signature}` with a space, which is what
solis-sensor 4.x sends and what production has been accepting. The mock
rejects any other spacing.

The body that is hashed must be the body that is sent. `aiohttp`'s `json=`
parameter inserts spaces after `:` and `,`, so the client posts the compact
bytes from `json.dumps(..., separators=(",", ":"))`.

`Date` must be within 15 minutes of the platform clock. Farther than that,
calls fail. In the field this is HTTP 408 (solis-sensor README), not a code
in Appendix 1. A VM with a wrong clock hits this immediately. We raise a
repair instead of a generic auth error.

### Endpoints this integration calls

All of these are in V2.0. Page and list calls use `pageNo` / `pageSize` up to
100 and follow `pages` until the list is exhausted.

| Path | Role |
| --- | --- |
| `/v1/api/userStationList` | Plants on the key. `id`, `stationName`, `timeZone`. |
| `/v1/api/stationDetail` | Plant energies, capacity, coordinates, timezone. Body `{"id": ...}`. |
| `/v1/api/stationDayEnergyList` | One civil day. Body needs `time` as `yyyy-MM-dd` and `stationIds`. Fallback when station detail omits today's import, export or load. |
| `/v1/api/inverterList` | Inverters for `stationId`. A call may return at most 100. Rows include `sn`, `id`, `state`, `pac`. |
| `/v1/api/inverterDetail` | Live inverter. Body `{"id", "sn"}`. This is the main telemetry document. |
| `/v1/api/alarmList` | Account alarms. `state` `0` pending, `1` processed, `2` restored. |
| `/v1/api/collectorList` | Data loggers. `state` `1` online, `2` offline. |
| `/v1/api/epmList` | Export power managers. `pEpmTotal` plus `pEpmTotalStr`. |

`inverterList.state`: `1` online, `2` offline, `3` alarm.
`dcInputType`: the document says value + 1 is the string count (`0` means one
string). `acOutputType`: `0` is single phase, anything else is three phase.

List payloads are not stable about where rows live. solis-sensor reads
`data.page.records`. Some samples put `records` directly on `data`. The parser
accepts both.

### Rate limit and refresh

The PDF says 2 requests per second **per interface**. That is a ceiling, not a
suggestion to run at it. solis-sensor sleeps about a second between calls and
comments that faster traffic produces HTTP 502. Operators on the upstream
tracker describe the practical limit as closer to one call per two seconds per
endpoint, which is what this client does by default (`endpoint_interval`,
minimum 0.5 so the documented 2/s ceiling stays available). HTTP 429 uses
`Retry-After` when it is a number of seconds or an HTTP date, otherwise 2
seconds, and then gives up. HTTP 5xx and timeouts retry with backoff. A 408
that includes a Date header is signed once more at that clock. Other 4xx
responses are not retried. Auth failures, a clock the server will not
correct, and `code` `1` are not retried.

Inverter samples land about every 5 minutes. Polling faster does not make the
cloud fresher. The default poll is 300 seconds and cannot be set below 60.
The default request timeout is 30 seconds. solis-sensor's old 10 second cap
caused discovery to fail after a restart (upstream issues 539 and 543). Issue
548 was the options flow dropping `request_timeout` on save. Both the options
flow and reconfigure persist it.

A full poll is one station list, then per plant a detail, a day-energy query
and an inverter list, then one detail per inverter, then alarms, collectors
and EPMs. Four inverters are on the order of fifteen calls. At one call per
two seconds that is about half a minute, which still fits in the 5 minute
poll.

### Units and other quirks

Companion fields are named `{value}Str` (`pac` / `pacStr`, `eTotal` /
`eTotalStr`). Some rows also carry `{value}Pec`, a multiplier. The V2.0
inverter sample uses `pacStr: "kW"`, `eTodayStr: "kWh"`, `uPv1Str: "V"`,
`pow1Str: "W"`. The same sample types `pacStr` as a number in the field table
and as a string in the example.

Rules implemented here:

- Power defaults to kW when the unit string is missing (`pac`, `batteryPower`,
  `psum`, `familyLoadPower`). An explicit `W` is watts. `kWp` (capacity) is
  treated as kW.
- Energy defaults to kWh. `MWh` × 1000 and `GWh` × 1 000 000 become kWh.
  A power unit on an energy field (`kW`, `MW`, `GW`) is read as the matching
  watt-hour unit. Negative energies are dropped so a `total_increasing`
  statistic cannot go backwards because of a sign error (upstream issue 508).
- The station sample labels `generatorTodayEnergyStr` as `kW`. An energy field
  whose only unit is `kW` is read as kWh.
- `*Pec` other than 1 multiplies the number before the unit scale.
- Null, `""` and the string `"null"` are missing. They are not coerced to 0.
  Energy sensors then keep the last good reading for the rest of that
  plant-local day, month, year or lifetime, so a missing sample cannot punch
  a zero into statistics (issues 382, 421, 533). A lifetime total also
  ignores a later reading that is smaller or that jumps by more than
  100 000 kWh.
- `storageBatteryCurrent` is signed. Battery power is given the sign of that
  current, which is the fix solis-sensor applied for issue 158. The PDF also
  has `batteryPowerZheng` / `batteryPowerFu` and a typo field `bstteryCurrent`.
  The typo is accepted as a fallback.
- `dataTimestamp` is documented as "under UTC+8" but the samples are Unix
  epoch milliseconds (`1687846773000`). The companion `dataTimestampStr` is the
  part rendered in China time. We parse the number as an epoch and ignore the
  string for the instant. Values that are not epoch-sized are ignored.
- Plant `timeZone` is a fixed numeric offset (`8`, `-9`, or `UTC+08:00`), not
  an IANA zone. Every energy sensor uses `total_increasing` and does not
  publish `last_reset`. The day, month and year boundary used to decide when
  a lower reading is a real reset follows Home Assistant's timezone when that
  zone is within an hour of the plant offset, which is the daylight-saving
  gap behind battery charge and discharge resetting around 23:00 (issue 533).
  A plant in another region keeps the numeric offset. Options can force an
  IANA name or `plant`. The cloud's own counter can still zero on the offset
  Solis stored; until the configured boundary, the sensor holds the previous
  value instead of publishing that early zero.
- Offline (`state` 2) with a fresh timestamp forces instantaneous power to 0.
  Leaving the previous `pac` in place is the stale-generation bug in issue
  366. A timestamp older than 15 minutes, or a missing timestamp, marks
  instantaneous sensors unavailable and the status sensor `stale`, while
  energy totals keep their last good value. A failed poll (timeout, 5xx, auth)
  marks the whole coordinator unavailable. Those are different states.
- `code` `1` with the Chinese message for abnormal data (`数据异常`) is an
  account-side fault. The upstream README says recreating the API key or a
  Solis ticket is what clears it. We surface a repair and do not spin on it.

### Error codes in Appendix 1

The appendix is short and does not match every failure people see:

| Code | Documented meaning |
| --- | --- |
| `R0000` | No authority |
| `B0001` | Bound to another user |
| `I0003` | SN required |
| `B0049` | Collector does not exist or is not visible |
| `I0000` | A required parameter is empty |
| `B0011` | User does not exist |
| `I0012` | Wrong account or password |

`I0012`, `R0000` and `B0011`, and HTTP 401/403, are authentication failures and
start a reauth flow. HTTP 408 is the clock. Anything else on 5xx is retried.

### Legacy Ginlong portal

solis-sensor 4.0.0 removed the Ginlong Platform 2.0 client. The 4.x README
says to stay on 3.x for a MyEvolve legacy portal. Tag `v3.13.2` still has it.
It is not the SolisCloud HMAC API.

- Login: `POST {portal}/cpro/login/validateLogin.json` with the portal
  username and password. Session cookie, not a key.
- Inverter list: `GET {portal}/cpro/epc/plantDevice/inverterListAjax.json`.
- Detail: `GET {portal}/cpro/device/inverter/goDetailAjax.json`.
- The host was whatever the user typed. People used `m.ginlong.com` and
  white-label MyEvolve hosts.
- Telemetry keys are opaque (`1ao` AC power, `1bd` energy today, `1bc`
  lifetime energy) inside `realTimeData*` blocks.

This rewrite does not call those paths. They are unauthenticated-looking GETs
behind a session cookie, the field names are not the v2 names, and Solis has
been moving accounts to SolisCloud. A migrated entry that only has a password
and no key cannot be refreshed; reauth asks for a key.

### Control API

solis-sensor 4.x can write inverter settings. It is off unless
`portal_control_api` is set, and it needs a portal password even when reads
use the key.

- `POST /v2/api/login` with `username` and `MD5(password)` returns a
  `csrfToken` (sometimes on the top-level object, not under `data`).
- `POST /v2/api/atRead` with `{inverterSn, cid}` and a `token` header reads one
  control. HMI firmware parsed from `hmiVersionAll` decides the CID set:
  below `4B00` and at/above `4B00` use different charge-slot layouts. Upstream
  issue 518 points at Pho3niX90/solis_modbus issue 94: newer HMI builds expose
  six charge/discharge slots. That is a register-map change, not a break of
  `/v1/api/inverterDetail`.
- `POST /v2/api/control` with `{inverterSn, cid, value}` writes. Slot times on
  older HMI are batched and only applied when an "update" CID is sent.

Upstream issue 437 reports that polling the control API drops remote access to
the inverter. Issue 473 reports controls disturbing the SOC graph SolisCloud
draws. The read loop in this integration never calls `/v2/api/atRead` or
`/v2/api/control`. A migrated entry that had control enabled gets a repair
saying the schedules already on the inverter are left alone. Force-charge SOC
and overdischarge SOC are exposed as sensors when `socChargingSet` and
`socDischargeSet` are present on `inverterDetail` (issue 526), not as numbers
that write.

There is no `write` method on `SolisTransport`. A later control implementation
has to be opt-in and has to stay off the five-minute read loop.

## Upstream issues

Open issues and the two open PRs on `hultenvp/solis-sensor` as of 2026-11-10.
"Fixed" means the behaviour is covered here, not that the upstream patch was
cherry-picked.

| # | What | Here |
| --- | --- | --- |
| 552 | `aiofiles<25` pin conflicts with Home Assistant | Fixed. No `aiofiles` dependency. |
| 551 (also PR 551) | Keep the last inverter list across `inverterList` failures | Fixed. A 5xx keeps the last list. Three empty-but-successful replies are tolerated, then the empty list is believed so a removed inverter can disappear. Detail failures keep the last detail. |
| 550 | Alarms as entities | Fixed. A problem binary sensor per inverter, plus code, message and advice attributes. Pending `state` `0` only. |
| 533 | Battery today resets around 23:00 | Partially. `last_reset` uses the plant's numeric offset and nulls are not written as 0. The cloud can still zero its own counter an hour early because the offset has no DST. |
| 526 | Max charge SOC missing | Fixed as read-only sensors when the detail payload has the fields. |
| 525 | Rework devices and sensors | Fixed. One device per plant, inverter, logger and EPM. Entity descriptions and `has_entity_name`. |
| 519 | MQTT discovery with an empty serial | Not applicable. This integration does not publish MQTT. |
| 518 | "Solis have changed API" | Not a cloud break. The linked reports are HMI slot maps. Documented under control and under local Modbus. |
| 517 (also PR 517) | Config flow rejects API-only keys | Fixed. Key id, secret and URL. Username is not collected. |
| 515 | Move onto the external `soliscloud-api` package | Not that package. A typed client lives in the integration so mypy can see it and Home Assistant's aiohttp session is the only HTTP stack. |
| 512 | Typo in `control_const.py` | Not applicable. That module is not shipped. |
| 510, 509 | Today's load missing | Fixed. Station `load_today` from `homeLoadTodayEnergy`, with `stationDayEnergyList` as fallback. |
| 503 | Set the inverter clock | Not implemented. It is a control write. |
| 482 | Energy dashboard figures wrong | Fixed for the unit bugs we could reproduce: kW/W/MWh/GWh and the kW-on-energy sample. |
| 475, 464, 458, 445, 443 | Feed-in limit, remote dispatch, storage-mode controls | Not implemented. See control API above. |
| 473 | Controls disturb the SOC graph | Avoided by never calling the control API during polls. |
| 471 | First-time setup confusion | Improved errors (auth vs clock vs timeout vs no plants). Not a substitute for Solis issuing a key. |
| 438 | Setup hangs and can take Home Assistant down | Fixed. Async client, bounded timeout, no blocking sleep on the event loop, `ConfigEntryNotReady` until the first poll works. |
| 437 | Control API drops remote access | Avoided. Control is not called. |
| 422 | Battery discharged today has no decimals | Fixed. Suggested display precision is 2 for energies. The state is not rounded to an integer. |
| 421, 382 | Energy today jumps or sticks around midnight | Fixed as far as the client can: no integer rounding, null stays unknown, `last_reset` is the plant-local midnight. |
| 366 | Power stays at the last watts when the inverter is offline | Fixed. Fresh offline samples report 0 W. Stale samples are unavailable. |
| 360 | Per-phase measurements missing | Fixed. Phases follow `acOutputType`. Single phase does not invent phase 2 and 3 from the zeros Solis still sends. |
| 325 | Cloud updates every 5 minutes while the portal page looks faster | Not a bug we can remove. Documented. Local Modbus is the way to poll faster, and it is a later phase. |
| 97 | Lifetime consumption should follow MWh when the unit says so | Fixed. `MWh` and `GWh` scale into kWh. |

Also guarded, from issues that upstream has since closed: configurable timeout
(539, 543), options keeping that timeout (548), no `control_const.py`
`split()` crash (535), and negative lifetime energy not stored as
`total_increasing` (508).

## Local access

Cloud is not the only way to read a Solis inverter, and for control it is the
worse way. What works depends on the stick more than on the inverter.

### Modbus TCP on the Solis stick

Solis's own developer page says Modbus TCP is available on the **S2-WL-ST**
with firmware at least `100101B4`, and that other loggers will follow. The
service article for that stick says:

- Default port **502**.
- Enabling the TCP server **disconnects the logger from SolisCloud** on the
  firmware that article describes. The logger's fallback address on a direct
  cable is `10.10.100.254`.
- Pho3niX90/solis_modbus later reports that some S2 firmware
  (`100121cf`, `100121d0`, `100141dx`) can do cloud and Modbus together, and
  that Solis support has to be asked for that build.

[fboundy/ha_solis_overview](https://github.com/fboundy/ha_solis_overview)
summarises the sticks people actually have:

| Stick | Link | Local protocol | Cloud at the same time | Notes |
| --- | --- | --- | --- | --- |
| S3-WIFI-ST | Wi-Fi | None without replacing firmware | Cloud only | Port 8899 is closed. Port 502 is not a documented feature. This is the stick shipped with most new systems. |
| S2-WL-ST | Wi-Fi and Ethernet | Modbus TCP port 502 | Not on older firmware; maybe on the builds above | Solis documents this one. |
| DLS-L | Ethernet, legacy | Modbus TCP. fboundy's overview says port **3389**; `ha_solis_modbus` says **8899** for older DLS-LAN. Both numbers are in the wild. | Often yes | No longer sold. Serial-number dependent. |
| DLS-W | Wi-Fi, legacy | Solarman V5 wrapping Modbus, port **8899** | Yes | Not Modbus TCP. Needs the logger serial. |

Firmware updates close this. Pho3niX90/solis_modbus issue 432 and the project
docs: after an OTA, S5-WiFi-ST and some S2-WL-ST units still answer ping and
still upload to SolisCloud, but **TCP 502 and TCP 80 are closed**. No
integration setting reopens a port the stick stopped listening on. The
workarounds that have actually worked are an RS485 bridge (Elfin EW11,
Waveshare) or a USB-RS485 adapter on the inverter's RS485 pins, leaving the
stick in place only if you are not sharing the same bus carelessly.

[hn/ginlong-solis](https://github.com/hn/ginlong-solis) is a firmware patch for
sticks that never opened a local port. It is unofficial, it can be overwritten
by the next Solis OTA, and it is not something this integration should perform.

### Register maps

The hybrid RS485 document (and the copies Solarman definitions were built
from) is Modbus RTU:

- 9600 baud, 8N1, slave address usually 1.
- At least 300 ms between frames. Recommended maximum 50 registers per frame.
- Function 04 reads input registers `30001-39999` (telemetry).
- Function 03 reads holding registers `40001-49999`.
- Functions 06 and 16 write holdings. Function 05 writes coils.
- Addresses in the Home Assistant maps are the PDU addresses (`33000` range and
  `43000` range), which are the document's 3xxxx/4xxxx numbers minus 1 in some
  tables and equal in others. fboundy and Solarman YAML use 33xxx/43xxx.
  Confirm against a known voltage register before trusting a new series.

There is not one map. Single-phase grid, three-phase grid and hybrid (and
HMI before/after `4B00`, and the six-slot HMI after `4C`) move the holding
registers for charge slots. `ha_solis_modbus` ships `solis_fb00.yaml` for HMI
at least FB00. Pho3niX90/solis_modbus tracks this continuously and is the
maintained map.

Writes are real inverter commands: storage mode, charge current, export
limit, time-of-use slots. A wrong map on a hybrid can change charge behaviour.
Read-only polling is the safe subset.

### Solarman V5

Older DLS-W and many rebadged Ginlong sticks speak Solarman V5 on **TCP 8899**.
The logger serial number is part of the frame. [pysolarmanv5](https://github.com/jmccrohan/pysolarmanv5)
implements it. Home Assistant projects:

- [StephanJoubert/home_assistant_solarman](https://github.com/StephanJoubert/home_assistant_solarman)
  with `inverter_definitions/solis_hybrid.yaml` (read, YAML register lists).
  Issue 63: S3-WIFI-ST does not open 8899, so this stack cannot see it.
- [davidrapan/ha-solarman](https://github.com/davidrapan/ha-solarman), a later
  Solarman integration with Solis definition files.
- NosIreland/solismod and similar small Python tools.

Refresh is whatever you poll. The stick still has to finish its own cloud
upload; aggressive polling collides with that and with the 300 ms frame gap.
S3 sticks are out. A Solis OTA that closes 8899 removes this path the same way
502 gets closed.

### Direct RS485

A Waveshare, Elfin or USB-RS485 adapter on the inverter COM port talks Modbus
RTU without the stick. Hardware people have used:

- USB-RS485 dongle on the COM plug (the Solis Wi-Fi stick uses an Exceedconn
  EC04681-style connector; adapters that fit the stick shell exist in the
  solis2mqtt / openenergymonitor write-ups).
- Waveshare RS485-to-Ethernet, or Elfin EW11, as a TCP gateway to the same
  pins. Pho3niX90 users who lost port 502 moved to these and the same
  integration worked.

Read and write both work, at the baud and gap above. Refresh can be a second
or two for a full hybrid map if frames are grouped, which is what
Pho3niX90/solis_modbus does (poll profiles down to about 2 seconds for a
reduced set). Reliability is the adapter and the wiring, not Solis's cloud,
and a stick firmware update does not close it. The risks are sharing the bus
with the stock stick (two masters), unplugging the stick and losing
SolisCloud, and writing holdings from a map for a different HMI.

### Projects, side by side

| Project | Hardware | Read | Write | Refresh | Fragile because |
| --- | --- | --- | --- | --- | --- |
| This integration, cloud | Any stick that still uploads | Yes | No, on purpose | About 5 min, poll default 5 min | Solis rate limit, 5 min samples, key and clock |
| hultenvp/solis-sensor 4.x | Same | Yes | Yes, control API, off by default | About 5 min, plus a control read per CID | Control polling (issue 437), `aiofiles` pin, username required |
| fboundy/ha_solis_modbus | DLS-LAN (8899 or 3389) or S2-WL-ST (502). Not S3. | Yes, YAML on the core Modbus integration | Yes, service calls | Core Modbus scan interval | One YAML file per HMI generation. Stick firmware can close the port. |
| Pho3niX90/solis_modbus | S2 / DLS / serial / Waveshare. Not a closed S3. | Yes | Yes, including remote dispatch on firmware that supports it | Profiles from a few seconds up | Port 502 closed by OTA (issue 432). HMI slot map keeps moving. MIT, actively maintained. |
| ha-solarman / pysolarmanv5 | DLS-W and other V5 sticks on 8899 | Yes | Depends on the definition | Poll interval | S3 has no 8899. OTA can close it. |
| Direct RS485 bridge | Adapter on the COM port | Yes | Yes | Seconds | Bus collisions with the stock stick. Wrong register map. |

### Should ha-solis grow a local mode?

Not in this change. Cloud is what solis-sensor users have today, and it is the
only no-hardware path for an S3-WIFI-ST. A local mode is worth a follow-up
for people who already run Pho3niX90 or a Waveshare bridge and want one
integration, but it should not try to replace Pho3niX90's register maintenance
on day one.

If it happens, the shape should be:

1. Another `SolisTransport` that returns the same `SolisSnapshot`. Entities
   already depend on that object, not on HTTP.
2. Read-only Modbus TCP (port 502) and a Solarman V5 reader (port 8899) as
   separate modules, selected in the config flow. Writes stay behind an
   explicit switch that is off by default, with the issue 437 warning.
3. The mock's fixture catalog (`tools/mock_soliscloud/catalog.py`) is the
   contract: single-phase grid, three-phase hybrid with battery, second
   three-phase inverter, stale and offline. A Modbus or V5 emulator should
   emit those same snapshot fields. It should not pretend to be the HTTP
   mock. The HTTP mock only exists because the cloud API is HTTP and it
   checks the HMAC.

The HTTP mock's `POST /__control` is a test hook, not a Solis API, and not the
extension point for Modbus.

## What Thomas should decide

- Keep domain `solis` (this PR) or rename so it can be installed beside
  upstream and accept new entity ids.
- Is the first real fleet mostly S3-WIFI-ST (cloud only) or S2 / RS485 (local
  is worth scheduling)?
- If there is a local phase, is it read-only at first?
- Should plant-level energy or the per-inverter energy be what the energy
  dashboard uses when a plant has several inverters? Both are exposed.
- The changeset in this PR will ask the release workflow for a version bump
  after merge. Delete it if `0.1.0` should be tagged by hand. This PR does
  not create a release or a tag.
- Brand art is original. Using the Solis wordmark needs a licence we do not
  have.
