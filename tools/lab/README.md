# Dongle lab

This lab boots a real Home Assistant container and points it at emulators for
every local logger the integration can talk to. The emulators use the old
3000-range string map. Lifetime energy is 21970 kWh on each device.

| Device | Address | What it speaks |
| --- | --- | --- |
| Solarman | 192.168.250.10 | V5 on TCP 8899, discovery on UDP 48899 |
| S2-WL-ST | 192.168.250.11 | Modbus TCP 502, port 80 names the Solis logger |
| Gateway | 192.168.250.12 | Modbus TCP 502, port 80 names Waveshare |
| Unnamed | 192.168.250.16 | Modbus TCP 502, port 80 does not name a device |
| RTU bridge | 192.168.250.13 | Raw Modbus RTU on TCP 4196 |
| S5-WiFi-ST | 192.168.250.14 | Modbus TCP 502, then the port can be closed |

Addresses are added to the loopback interface. Home Assistant uses host
networking so it shares those addresses and UDP discovery. `POST http://127.0.0.1:8871/__control`
changes a device. The body is JSON, for example
`{"device":"solarman","night":true}` or
`{"device":"s5","mode":"closed"}`. Modes include `ok`, `reset`, `silent`,
`exception`, `wrong_slave`, `short`, `echo` and `closed`.

## Run the audit

From the repo root, with Docker available:

```bash
python3.14 tools/lab/run_lab.py 2026.11.1 2025.10.0
```

`2026.11.1` is the image the repo's other lab pins. `2025.10.0` is the minimum
in `hacs.json`. The script creates `lab0`, starts the emulators, and for each
version:

1. Installs the integration from `main`, creates a Solarman entry and an
   S2-WL-ST entry, then switches the container to this branch and checks that
   the same entities keep their values.
2. Drives auto-detect, the port-80 identification question, manual RTU, and
   an S5 whose port later closes.
3. Stops a stick (night), moves the Solarman address, resets TCP, sends a
   Modbus exception and a short frame, and checks energy does not drop.
4. Restarts Home Assistant and downloads diagnostics.

A JSON report is written to `/tmp/ha-solis-lab-report.json`.

The emulator can also be started on its own, after the dummy addresses exist:

```bash
sudo /usr/sbin/ip addr add 192.168.250.10/32 dev lo
sudo /usr/sbin/ip addr add 192.168.250.11/32 dev lo
sudo /usr/sbin/ip addr add 192.168.250.12/32 dev lo
sudo /usr/sbin/ip addr add 192.168.250.13/32 dev lo
sudo /usr/sbin/ip addr add 192.168.250.14/32 dev lo
sudo /usr/sbin/ip addr add 192.168.250.15/32 dev lo
sudo /usr/sbin/ip addr add 192.168.250.16/32 dev lo
sudo python3 tools/lab/dongles.py
```

`tools/lab/docker-compose.yml` is the Home Assistant service the script starts.
Set `HA_VERSION` and `HA_CONFIG` before `docker compose -f tools/lab/docker-compose.yml up -d`.
