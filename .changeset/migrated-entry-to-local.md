---
"ha-solis-release-tools": minor
---

Moving between SolisCloud and a local stick, in either direction, and replacing a stick keep your entities and their history. A poll of the stick no longer deletes the plant, export meter or other inverter devices that only SolisCloud reports, so a migrated solis-sensor entry can move to a stick (Reconfigure → Local stick, then Connection → Local stick only) without losing plant history. A poll interval still on the default of the connection you leave now follows the new one (60 seconds for a stick, 300 seconds for SolisCloud only), and swapping a web-page stick for a Modbus stick deletes the saved web password.
