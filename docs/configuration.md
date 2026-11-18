# Configuration

Add the integration under Settings → Devices & services → Add integration →
Solis. Not sure which connection to choose? See
[Which connection should I pick?](index.md#which-connection-should-i-pick)

## First step: how to read the inverter

The first screen is a menu with three choices:

- **SolisCloud only.** Asks for the API URL, key ID and secret, then which
  plants to add. Works for every stick, from anywhere.
- **Local stick only.** Asks for the stick on your network. No SolisCloud
  account is needed. The stick only answers while the inverter is producing.
- **Local stick with SolisCloud backup.** Asks for the SolisCloud key and
  plants first, then the stick. The stick is read first; SolisCloud fills in
  when the stick is busy and supplies values the stick does not have.

### SolisCloud form

| Field | Meaning |
| --- | --- |
| API URL | SolisCloud host, including the port. The default `https://www.soliscloud.com:13333` is the international service. Keys issued by `https://v3.soliscloud.com:13333` keep that host. A public `http://` URL is stored as `https://`. |
| API key ID | The key ID from SolisCloud API management. Not your portal username. |
| API key secret | Shown once when the key is created. Stored in the config entry, never logged, redacted in diagnostics. |
| Plants | The next step. Only the selected plants are read. |

One SolisCloud account can be added once as SolisCloud only. Several local
entries with SolisCloud backup may share one key, because each has its own
stick.

### Local stick form

The stick has to be on, so set it up while the inverter is producing.
Solarman / IGEN sticks found on your network are listed in the address
field; you can also type an address.

| Field | Meaning |
| --- | --- |
| Logger type | **Auto-detect** works for most sticks. You can also choose: Solarman / IGEN Wi-Fi stick (DLS-W, DLS-L, LSW); Solis S2-WL-ST; RS485 gateway in Modbus TCP mode (Waveshare, Elfin, USR); Solis S5-WiFi-ST (experimental); RS485 gateway in transparent mode (experimental); Solis stick with only a status web page (S3-class). |
| IP address | The stick's address on your network. `10.10.100.254` is the stick's own setup network, not your home network. Reserve the address in your router. |
| Inverter serial | Optional. The serial on the inverter label or in SolisCloud, for example `1031000000000001`. Leave it empty to read it from the stick, or from SolisCloud when that is the backup. Typing it makes entity IDs match SolisCloud. |

**Advanced options** is a collapsed section. You only need it when
auto-detect cannot find the stick or for a gateway on a non-standard port:

| Field | Meaning |
| --- | --- |
| Logger serial | Solarman stick: the number on the label, filled in from discovery when the stick answers. S2-WL-ST and S5: the serial in the setup Wi-Fi name (`D_<serial>` or `Solis_<serial>`). Gateway: any label, or empty. |
| Port | Empty uses the usual port of the selected logger type. A typed port always wins. |
| Modbus slave | Empty tries 1, 2 and 3. Solis inverters use 1. |
| RS485 gateway model | Only for a gateway in transparent mode with an empty Port. Sets the factory port: Elfin / Hi-Flying 8899, Waveshare 4196, USR 26, ESPHome stream server 6638. |

Auto-detect tries Modbus TCP on port 502, then Solarman V5 on 8899, then a
Solis status web page on port 80. Port 502 is stored as an S2-WL-ST when the
device's web page names the Solis stick, as a Modbus TCP gateway when it
names Waveshare, Elfin, Hi-Flying or USR, and otherwise the flow asks
**Which device is this?** with those two choices.

### Web password step

This step only appears for a stick that only serves a status web page, and
only when that page rejected the factory password. The user is always
`admin`. Leave the field empty to try the factory passwords `123456789` and
then `admin` (sticks made by IGEN/Solarman for Solis ship with `admin`).
Some firmware uses your Wi-Fi password instead. The password is stored in the config entry and
redacted in diagnostics.

## Discovery

Solarman / IGEN sticks are discovered two ways:

- While you fill in the local stick form, the integration sends the
  Solarman UDP broadcast and lists the sticks that answer.
- Home Assistant's DHCP watcher notices a device with MAC prefix
  `E8:FD:F8` or `BC:54:F9` (IGEN). If it answers the Solarman broadcast, a
  **Solarman stick found** card appears under Discovered. Opening it asks
  whether to read it as **Local stick only** or **Local stick with
  SolisCloud backup**, then continues with the forms above.

A stick that is already configured is not offered again. When its address
changes, DHCP updates the existing entry instead (see
[The stick changed address](troubleshooting.md#the-stick-changed-address)).
An S2-WL-ST, an S5, a web-page stick and RS485 gateways do not answer the
Solarman broadcast: type their address.

## Reconfigure

Reconfigure on the Solis entry opens a menu:

| Choice | What it changes |
| --- | --- |
| SolisCloud account | API URL, key ID or secret. Leave the secret empty to keep the saved one. On a local-only entry this adds SolisCloud as a backup and then asks for the plants. |
| Plants | Which plants a SolisCloud entry reads. Not shown on a local-only entry. |
| Local stick | The stick form, filled in with the saved values. A SolisCloud-only entry that gains a stick becomes local with SolisCloud backup. An empty web password keeps the saved one, but only for the same address. |
| Connection | Switch between SolisCloud only, Local stick only and Local stick with SolisCloud backup. Shown when the entry has both a key and a stick. The settings of the source you stop using are kept. |

Each change reloads the entry.

## Reauthentication

When SolisCloud rejects the key, Home Assistant shows a **Reauthenticate**
card. Enter a current key ID and secret; a key from a different account is
refused. When a web-page stick rejects its password, the same card asks for
the stick's web password instead. An entry with both asks which login was
rejected.

## Options

Configure on the Solis entry. Saving reloads the entry.

| Field | Meaning |
| --- | --- |
| Poll interval (seconds) | SolisCloud only: default 300, never faster than 60, because cloud data changes about every five minutes. With a local stick: default 60, never faster than 30. Maximum 3600. |
| Plant timezone | Used for "today", "this month" and "this year". **Automatic** (the default) uses the plant's own time zone from SolisCloud when it reports one, otherwise Home Assistant's time zone when that is within an hour of the plant offset. **Plant offset from Solis** always uses the numeric offset Solis sends. You can also type an IANA name such as `Europe/London`. |

**Advanced options** (collapsed):

| Field | Meaning |
| --- | --- |
| Request timeout (seconds) | How long to wait for one SolisCloud call. Default 30, range 5 to 120. |
| Minimum gap per endpoint (seconds) | Minimum time between calls to the same API path. Default 2, range 0.5 to 30. The SolisCloud limit is 2 calls per second per path. |

## How the local connection behaves

A stick powered by the inverter disappears from your network at night. The
integration then keeps the energy totals, shows live power as 0 and sets
Data source to `asleep`. No repair is opened.

A stick that accepts the connection and then resets it is not asleep: the
last values are held (or SolisCloud is used when it is the backup), Data
source reads `degraded`, and the stick is tried again after at least 10
seconds, then less often, up to about 10 minutes. A repair opens only after
10 minutes of this. See [Troubleshooting](troubleshooting.md).

With SolisCloud as backup, the stick wins for the values it has. SolisCloud
fills the rest, such as month and year energy, phases, PV strings, the series
name and the plant. A SolisCloud sample older than 15 minutes only supplies
totals and names, not live readings.

### Solis web page stick

Some S3-class sticks have no Modbus or Solarman port, only a status web page
on port 80. The integration reads `inverter.cgi`, or `status.html` on sticks
made by IGEN/Solarman for Solis. The page gives power, temperature, energy
today and total, firmware and the alarm word. The stick reads the inverter
about every five minutes and serves that sample until the next one, so the
values step rather than move smoothly. The integration reads the page every
poll (60 seconds by default), so a new sample shows up within about a minute.

With SolisCloud as backup, the cloud is read at most every five minutes and
fills month and year energy, frequency, phases, PV strings and the series
name. Without SolisCloud those values stay empty.

### Unique IDs

A SolisCloud-only entry is identified by its API key ID. An entry with a
stick is identified by the stick: the Solarman serial; `s2:` plus the
S2-WL-ST or S5 serial (or the inverter serial when no stick serial was
typed); `tcp:` or `rtu:` plus the gateway label or inverter serial. The
same stick cannot be added twice.
