#!/usr/bin/env python3
"""Read the Solarman stick on the LAN and print the string-inverter registers.

The inverter has to be producing. At night the stick is powered off and this
script times out. From a checkout of this repository, on a machine that can
reach the logger:

    python scripts/read_solis_stick.py
    python scripts/read_solis_stick.py --host 192.168.0.105 --serial 1902137671

This does not read registers at 3050 or above. On the bench stick that
range resets the local session. Pass the SolisCloud inverter serial to the
integration instead, or let local-and-cloud mode copy it.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from custom_components.solis.errors import (  # noqa: E402
    SolisLoggerBusy,
    SolisLoggerOffline,
    logger_failure_text,
)
from custom_components.solis.registers import decode_string, serial_from_words  # noqa: E402
from custom_components.solis.solarman import SolarmanClient  # noqa: E402

BLOCKS = ((3000, 20), (3020, 20), (3040, 10))


async def _read(host: str, serial: int) -> None:
    client = SolarmanClient(host, serial, timeout=8, gap=0.35)
    words: dict[int, int] = {}
    try:
        for address, count in BLOCKS:
            values = await client.read_input(1, address, count)
            for offset, value in enumerate(values):
                words[address + offset] = value
            print(f"{address}: {values}")
    finally:
        await client.close()
    learned = serial_from_words(words) or f"L{serial}"
    inverter = decode_string(words, serial=learned, logger_serial=str(serial))
    print(
        f"serial {inverter.serial}  total {inverter.energy_total_kwh} kWh  "
        f"today {inverter.energy_today_kwh} kWh  "
        f"power {inverter.ac_power_w} W  "
        f"pv {inverter.strings[0].voltage_v if inverter.strings else None} V / "
        f"{inverter.strings[0].current_a if inverter.strings else None} A  "
        f"grid {inverter.phases[0].voltage_v if inverter.phases else None} V  "
        f"temp {inverter.temperature_c} C  "
        f"freq {inverter.frequency_hz} Hz  "
        f"state {inverter.state}"
    )


def main() -> None:
    """Read one stick. Defaults are the logger this integration was checked against."""
    parser = argparse.ArgumentParser(description="Read a Solarman V5 Solis stick")
    parser.add_argument("--host", default="192.168.0.105")
    parser.add_argument("--serial", type=int, default=1902137671)
    args = parser.parse_args()
    try:
        asyncio.run(_read(args.host, args.serial))
    except (SolisLoggerOffline, SolisLoggerBusy) as err:
        print(logger_failure_text(args.host, err))
        raise SystemExit(1) from err


if __name__ == "__main__":
    main()
