---
"ha-solis-release-tools": minor
---

Read a Solis stick that has no Modbus or Solarman port but a status web page (some S3-class firmware). The new Solis web page logger kind reads power, temperature, energy today and total, firmware and alarm from the stick, which refreshes them about every five minutes like SolisCloud. Auto-detect offers it when ports 502 and 8899 are closed and port 80 asks for the Solis login. With SolisCloud also configured, the web page wins and the cloud fills month and year energy, frequency, phases, strings and the series name, read at most every five minutes. A wrong web password opens a repair after 10 minutes.
