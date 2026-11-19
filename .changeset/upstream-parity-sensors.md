---
"ha-solis-release-tools": minor
---

New SolisCloud sensors that solis-sensor had: grid import and export this month and this year, battery charged and discharged this month and this year, backup (EPS) load power and today's backup energy, smart-load port power and today's energy (disabled by default), and per-phase meter voltage, current, apparent power and reactive power (disabled by default). Each is added once the inverter reports the value, and solis-sensor's entities for these readings keep their entity IDs and history when you migrate.
