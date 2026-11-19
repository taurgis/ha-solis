---
"ha-solis-release-tools": minor
---

A migrated solis-sensor entry (or any SolisCloud entry) can move to a local stick without losing plant history. A poll of the stick no longer deletes the plant, export meter or other inverter devices that only SolisCloud reports, so Reconfigure → Local stick and Connection → Local stick only keep those entities and their history. Adding a stick also moves a poll interval still on the 300-second SolisCloud default to the 60-second local default.
