"""SolisCloud readings that hultenvp/solis-sensor exposed and this integration now reads too."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .test_fix_cloud_parsing import _CAPTURE_DISCHARGE, _entry
from .test_fix_runtime_entities import (
    SERIAL,
    _inverter,
    _local,
    _local_entry,
    _setup,
    _Transport,
)
from custom_components.solis.cloud_fill import fill_from_cloud
from custom_components.solis.const import DOMAIN
from custom_components.solis.migration import async_migrate_entity_ids, legacy_unique_id
from custom_components.solis.models import (
    Phase,
    snapshot_from_dict,
    snapshot_to_dict,
)
from custom_components.solis.parser import parse_inverter
from custom_components.solis.sensor import _phase_value
from custom_components.solis.units import to_volt_amperes

_DETAIL: dict[str, Any] = {
    **_CAPTURE_DISCHARGE,
    "acOutputType": 1,
    "batteryMonthChargeEnergy": 84.2,
    "batteryMonthChargeEnergyStr": "kWh",
    "batteryMonthDischargeEnergy": 80.1,
    "batteryMonthDischargeEnergyStr": "kWh",
    "batteryYearChargeEnergy": 1.25,
    "batteryYearChargeEnergyStr": "MWh",
    "batteryYearDischargeEnergy": 1.2,
    "batteryYearDischargeEnergyStr": "MWh",
    "gridPurchasedMonthEnergy": 120.5,
    "gridPurchasedMonthEnergyStr": "kWh",
    "gridSellMonthEnergy": 310.0,
    "gridSellMonthEnergyStr": "kWh",
    "gridPurchasedYearEnergy": 1.8,
    "gridPurchasedYearEnergyStr": "MWh",
    "gridSellYearEnergy": 3.1,
    "gridSellYearEnergyStr": "MWh",
    "bypassLoadPower": 0.45,
    "bypassLoadPowerStr": "kW",
    "backupTodayEnergy": 2.3,
    "backupTodayEnergyStr": "kWh",
    "backup2Power": 0.8,
    "backup2PowerStr": "kW",
    "backup2TodayEnergy": 1.1,
    "backup2TodayEnergyStr": "kWh",
    "uA": 231.5,
    "iA": 4.2,
    "aLookedPower": 980,
    "aReactivePower": -120,
    "uB": 232.0,
    "iB": 4.0,
    "bLookedPower": 0.95,
    "bLookedPowerStr": "kVA",
    "bReactivePower": 0.11,
    "bReactivePowerStr": "kvar",
    "uC": 230.0,
    "iC": 3.9,
    "cLookedPower": 900,
    "cReactivePower": 100,
}


def test_month_year_backup_and_smart_load_are_parsed() -> None:
    """Month and year counters, backup and smart-load ports come from inverter detail."""
    parsed = parse_inverter(_DETAIL)
    assert parsed.battery_charge_month_kwh == pytest.approx(84.2)
    assert parsed.battery_discharge_month_kwh == pytest.approx(80.1)
    assert parsed.battery_charge_year_kwh == pytest.approx(1250.0)
    assert parsed.battery_discharge_year_kwh == pytest.approx(1200.0)
    assert parsed.grid_import_month_kwh == pytest.approx(120.5)
    assert parsed.grid_export_month_kwh == pytest.approx(310.0)
    assert parsed.grid_import_year_kwh == pytest.approx(1800.0)
    assert parsed.grid_export_year_kwh == pytest.approx(3100.0)
    assert parsed.backup_power_w == pytest.approx(450.0)
    assert parsed.backup_today_kwh == pytest.approx(2.3)
    assert parsed.smart_load_power_w == pytest.approx(800.0)
    assert parsed.smart_load_today_kwh == pytest.approx(1.1)


def test_meter_phase_readings_are_parsed() -> None:
    """The meter's per-phase voltage, current, apparent and reactive power land on each phase."""
    phases = {phase.index: phase for phase in parse_inverter(_DETAIL).phases}
    assert phases[1].meter_voltage_v == pytest.approx(231.5)
    assert phases[1].meter_current_a == pytest.approx(4.2)
    assert phases[1].apparent_power_va == pytest.approx(980.0)
    assert phases[1].reactive_power_var == pytest.approx(-120.0)
    assert phases[2].apparent_power_va == pytest.approx(950.0)
    assert phases[2].reactive_power_var == pytest.approx(110.0)
    assert phases[3].meter_voltage_v == pytest.approx(230.0)
    single = parse_inverter({**_DETAIL, "acOutputType": 0}).phases
    assert [phase.index for phase in single] == [1]
    bare = parse_inverter(_CAPTURE_DISCHARGE)
    assert bare.backup_power_w is None
    assert bare.grid_import_month_kwh is None
    assert all(phase.reactive_power_var is None for phase in bare.phases)


def test_volt_amperes_follow_the_unit_field() -> None:
    """VA and var pass through, the kilo forms scale, an unknown unit is dropped."""
    assert to_volt_amperes(5, None) == 5.0
    assert to_volt_amperes(1.5, "kVA") == 1500.0
    assert to_volt_amperes(0.2, "kvar", "10") == pytest.approx(2000.0)
    assert to_volt_amperes(5, "kWh") is None
    assert to_volt_amperes(None, "VA") is None


def test_new_fields_survive_the_stored_snapshot() -> None:
    """The restart cache keeps the new counters and meter readings."""
    inverter = parse_inverter(_DETAIL)
    snapshot = _local()
    snapshot = replace(snapshot, inverters=(replace(inverter, data_timestamp=None),))
    restored = snapshot_from_dict(snapshot_to_dict(snapshot)).inverters[0]
    assert restored.grid_export_year_kwh == pytest.approx(3100.0)
    assert restored.smart_load_power_w == pytest.approx(800.0)
    assert restored.phases == inverter.phases


def test_phase_meter_values_follow_the_inverter_state() -> None:
    """Meter readings are unknown while offline or stale; the grid runs on without the inverter."""
    now = datetime.now(UTC)
    phase = Phase(
        index=1,
        voltage_v=230.0,
        current_a=5.0,
        power_w=1150.0,
        meter_voltage_v=231.0,
        meter_current_a=4.8,
        apparent_power_va=1100.0,
        reactive_power_var=-90.0,
    )
    live = _inverter(phases=(phase,))
    assert _phase_value(live, "phase_meter_voltage", 1, now) == 231.0
    assert _phase_value(live, "phase_meter_current", 1, now) == 4.8
    assert _phase_value(live, "phase_apparent_power", 1, now) == 1100.0
    assert _phase_value(live, "phase_reactive_power", 1, now) == -90.0
    offline = replace(live, state=2)
    assert _phase_value(offline, "phase_reactive_power", 1, now) is None
    assert _phase_value(offline, "phase_meter_voltage", 1, now) is None
    # The inverter's own output current on the phase is known: zero.
    assert _phase_value(offline, "phase_current", 1, now) == 0.0
    stale = replace(live, data_timestamp=now - timedelta(days=1))
    assert _phase_value(stale, "phase_meter_voltage", 1, now) is None
    assert _phase_value(stale, "phase_meter_current", 1, now) is None
    assert _phase_value(stale, "phase_apparent_power", 1, now) is None


def test_an_old_cloud_fill_drops_backup_and_smart_load_power() -> None:
    """Only counters survive a cloud sample that is too old to be live."""
    cloud = _local()
    donor = replace(
        cloud.inverters[0], backup_power_w=450.0, smart_load_power_w=800.0, backup_today_kwh=2.0
    )
    cloud = replace(cloud, inverters=(donor,))
    old = fill_from_cloud(cloud, cloud, serial=SERIAL, fresh=False).inverters[0]
    assert old.backup_power_w is None
    assert old.smart_load_power_w is None
    assert old.backup_today_kwh == 2.0
    fresh = fill_from_cloud(cloud, cloud, serial=SERIAL, fresh=True).inverters[0]
    assert fresh.backup_power_w == 450.0


async def test_reported_readings_become_entities(hass: HomeAssistant) -> None:
    """Each new sensor is added once reported; smart load and meter sensors start disabled."""
    entry = _local_entry(hass)
    await _setup(hass, entry, _Transport(_local()))
    registry = er.async_get(hass)

    def entity(key: str) -> str | None:
        return registry.async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_{key}")

    assert entity("grid_import_month") is None
    assert entity("phase_1_reactive_power") is None
    coordinator = entry.runtime_data
    base = coordinator.data.inverters[0]
    reported = replace(
        base,
        product_model="2",
        battery_soc=80.0,
        battery_charge_month_kwh=84.2,
        grid_import_month_kwh=120.5,
        grid_export_year_kwh=3100.0,
        backup_power_w=450.0,
        backup_today_kwh=2.3,
        smart_load_power_w=0.0,
        phases=(replace(base.phases[0], reactive_power_var=-120.0, meter_voltage_v=231.0),),
    )
    coordinator.async_set_updated_data(replace(coordinator.data, inverters=(reported,)))
    await hass.async_block_till_done()
    for key, state in (
        ("grid_import_month", "120.5"),
        ("grid_export_year", "3100.0"),
        ("backup_power", "450.0"),
        ("backup_today", "2.3"),
        ("battery_charge_month", "84.2"),
    ):
        entity_id = entity(key)
        assert entity_id is not None, key
        assert hass.states.get(entity_id).state == state, key  # type: ignore[union-attr]
    for key in ("grid_export_month", "smart_load_today", "battery_discharge_year"):
        assert entity(key) is None, key
    for key in ("smart_load_power", "phase_1_reactive_power", "phase_1_meter_voltage"):
        entity_id = entity(key)
        assert entity_id is not None, key
        row = registry.async_get(entity_id)
        assert row is not None and row.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert entity("phase_1_apparent_power") is None
    # A later poll without the readings keeps the entities.
    coordinator.async_set_updated_data(replace(coordinator.data, inverters=(base,)))
    await hass.async_block_till_done()
    assert entity("backup_power") is not None
    assert entity("phase_1_reactive_power") is not None


async def test_upstream_labels_for_the_new_sensors_migrate(hass: HomeAssistant) -> None:
    """Upstream entities for these readings keep their history."""
    entry = _entry(hass)
    registry = er.async_get(hass)
    cases = {
        "Monthly Energy Charged": "SN1_battery_charge_month",
        "Yearly Energy Discharged": "SN1_battery_discharge_year",
        "Monthly Grid Energy Purchased": "SN1_grid_import_month",
        "Yearly On-grid Energy": "SN1_grid_export_year",
        "Backup Load Power": "SN1_backup_power",
        "Daily Backup Energy": "SN1_backup_today",
        "Smart Load Power": "SN1_smart_load_power",
        "Daily Smart Load Energy": "SN1_smart_load_today",
        "Grid Phase2 Apparent Power": "SN1_phase_2_apparent_power",
        "Grid Phase3 Reactive Power": "SN1_phase_3_reactive_power",
        "Meter item A volt": "SN1_phase_1_meter_voltage",
        "Meter item C current": "SN1_phase_3_meter_current",
    }
    rows = {
        label: registry.async_get_or_create(
            "sensor", DOMAIN, legacy_unique_id("SN1", "Roof", label), config_entry=entry
        )
        for label in cases
    }
    async_migrate_entity_ids(hass, entry.entry_id, "Roof", {"SN1"}, {"SN1": "P"})
    for label, new in cases.items():
        assert registry.async_get_entity_id("sensor", DOMAIN, new) == rows[label].entity_id
