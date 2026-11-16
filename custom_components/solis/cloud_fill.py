"""Fill what a web-page stick cannot report from a SolisCloud sample.

The stick page carries power, temperature, energy today and total, the
firmware and an alarm word. SolisCloud has the same values with the same
delay, plus month and year energy, frequency, phases, PV strings, the series
name, the plant and its alarms. The page values win for what they cover; the
cloud supplies the rest. Live fields from an old cloud sample are dropped
instead of shown as current.
"""

from __future__ import annotations

from dataclasses import replace

from .models import InverterSnapshot, SolisSnapshot


def match_inverter(cloud: SolisSnapshot, serial: str) -> InverterSnapshot | None:
    """Pick the cloud inverter for this stick. One inverter on the account is enough."""
    wanted = serial.strip().casefold()
    for inverter in cloud.inverters:
        if wanted and inverter.serial.strip().casefold() == wanted:
            return inverter
    if len(cloud.inverters) == 1 and not wanted:
        return cloud.inverters[0]
    return None


def fill_from_cloud(
    web: SolisSnapshot, cloud: SolisSnapshot, *, serial: str, fresh: bool
) -> SolisSnapshot:
    """Return the web snapshot with every field it lacks taken from ``cloud``.

    ``fresh`` is false for a cloud sample older than the freshness window: only
    the slow counters and names are kept then, not live readings or alarms.
    """
    donor = match_inverter(cloud, serial)
    if donor is None or not web.inverters:
        return web
    page = web.inverters[0]
    merged = replace(
        donor,
        serial=page.serial,
        device_id=page.serial,
        name=page.serial,
        model_code=donor.model_code if donor.model_code is not None else page.model_code,
        hmi_version=page.hmi_version or donor.hmi_version,
        state=page.state,
        data_timestamp=page.data_timestamp,
        ac_power_w=page.ac_power_w,
        temperature_c=page.temperature_c,
        energy_today_kwh=page.energy_today_kwh,
        energy_total_kwh=page.energy_total_kwh,
        data_source=page.data_source,
        unreachable=False,
    )
    if not fresh:
        merged = replace(
            merged,
            frequency_hz=None,
            battery_power_w=None,
            battery_soc=None,
            battery_current_a=None,
            grid_power_w=None,
            consumption_power_w=None,
            phases=(),
            strings=(),
        )
    return replace(
        web,
        stations=cloud.stations or web.stations,
        inverters=(merged, *web.inverters[1:]),
        alarms=cloud.alarms if fresh else (),
        collectors=cloud.collectors or web.collectors,
        epms=cloud.epms if fresh else (),
        # Without live cloud data the plant's devices are not known to be gone.
        partial=not fresh,
    )
