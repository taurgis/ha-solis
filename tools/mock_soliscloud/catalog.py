"""Recorded-style SolisCloud bodies for the mock.

Numbers are the shapes the Platform API v2 samples use: companion ``*Str``
units, ``*Pec`` multipliers, epoch-millisecond timestamps, and one offline
inverter whose ``pac`` must not keep being reported as generation.
"""

from __future__ import annotations

from typing import Any

NORTH = "1900000000000001001"
BARN = "1900000000000001002"

NORTH_SERIAL = "1011A021B150001"
HYBRID_SERIAL = "1102C021B150002"
GRID_SERIAL = "1203D021B150003"
STALE_SERIAL = "1304E021B150004"

KEY_ID = "test-key"
SECRET = "test-secret"


def stations() -> list[dict[str, Any]]:
    """Return userStationList rows."""
    return [
        {
            "id": NORTH,
            "stationName": "North Roof",
            "timeZone": 0,
            "timeZoneStr": "UTC+00:00",
            "cityStr": "Example City",
        },
        {
            "id": BARN,
            "stationName": "Barn",
            "timeZone": 8,
            "timeZoneStr": "UTC+08:00",
            "cityStr": "Example County",
        },
    ]


def station_details() -> dict[str, dict[str, Any]]:
    """Return stationDetail objects keyed by id."""
    return {
        NORTH: {
            "id": NORTH,
            "stationName": "North Roof",
            "sno": "North Roof",
            "timeZone": 0,
            "timeZoneStr": "UTC+00:00",
            "dayEnergy": 6.0,
            "dayEnergyStr": "kWh",
            "gridPurchasedDayEnergy": 1.5,
            "gridPurchasedDayEnergyStr": "kWh",
            "gridSellDayEnergy": 4.0,
            "gridSellDayEnergyStr": "kWh",
            "homeLoadTodayEnergy": 3.2,
            "homeLoadTodayEnergyStr": "kWh",
            "capacity": 5,
            "capacityStr": "kWp",
            "latitude": 51.5,
            "longitude": -0.12,
            "cityStr": "Example City",
        },
        BARN: {
            "id": BARN,
            "stationName": "Barn",
            "timeZone": 8,
            "dayEnergy": 10,
            "dayEnergyStr": "kWh",
            "gridPurchasedDayEnergy": 0.4,
            "gridPurchasedDayEnergyStr": "kWh",
            "gridSellDayEnergy": 2.1,
            "gridSellDayEnergyStr": "kWh",
            "homeLoadTodayEnergy": 6.5,
            "homeLoadTodayEnergyStr": "kWh",
            "capacity": 15,
            "capacityStr": "kW",
            "latitude": 31.2,
            "longitude": 121.5,
        },
    }


def _common(serial: str, device_id: str, station_id: str, name: str) -> dict[str, Any]:
    return {
        "id": device_id,
        "sn": serial,
        "stationId": station_id,
        "name": name,
        "collectorSn": f"COLLECTOR{station_id[-4:]}",
        "fac": 50.01,
        "facStr": "Hz",
        "inverterTemperature": 42.5,
        "inverterTemperatureStr": "C",
        "dataTimestamp": 0,
    }


def inverter_details() -> dict[str, dict[str, Any]]:
    """Return inverterDetail objects keyed by serial."""
    north = _common(NORTH_SERIAL, "2001", NORTH, "North Roof")
    north.update(
        {
            "productModel": "1",
            "series": "S6-GR1P5K",
            "machine": "S6-GR1P5K",
            "state": 1,
            "acOutputType": 0,
            "dcInputType": 0,
            "pac": 1.25,
            "pacStr": "kW",
            "pacPec": "1",
            "eToday": 4.321,
            "eTodayStr": "kWh",
            "eMonth": 80.5,
            "eMonthStr": "kWh",
            "eYear": 900,
            "eYearStr": "kWh",
            "eTotal": 12.5,
            "eTotalStr": "MWh",
            "uAc1": 230.5,
            "uAc1Str": "V",
            "iAc1": 5.42,
            "iAc1Str": "A",
            "uAc2": 0,
            "uAc2Str": "V",
            "iAc2": 0,
            "iAc2Str": "A",
            "uPv1": 320.0,
            "uPv1Str": "V",
            "iPv1": 4.1,
            "iPv1Str": "A",
            "pow1": 1312,
            "pow1Str": "W",
            "psum": 0.2,
            "psumStr": "kW",
            "familyLoadPower": 0.4,
            "familyLoadPowerStr": "kW",
            "hmiVersionAll": "4C0010",
        }
    )
    hybrid = _common(HYBRID_SERIAL, "2002", BARN, "Barn Hybrid")
    hybrid.update(
        {
            "productModel": "2",
            "series": "S6-EH3P10K",
            "machine": "S6-EH3P10K",
            "state": 1,
            "acOutputType": 1,
            "dcInputType": 1,
            "pac": 3.5,
            "pacStr": "kW",
            "eToday": 8.5,
            "eTodayStr": "kWh",
            "eTotal": 20,
            "eTotalStr": "kWh",
            "uAc1": 235,
            "uAc1Str": "V",
            "uAc2": 236,
            "uAc2Str": "V",
            "uAc3": 234,
            "uAc3Str": "V",
            "iAc1": 5.0,
            "iAc1Str": "A",
            "iAc2": 5.1,
            "iAc2Str": "A",
            "iAc3": 4.9,
            "iAc3Str": "A",
            "pA": 1200,
            "pAStr": "W",
            "pB": 1100,
            "pBStr": "W",
            "pC": 1200,
            "pCStr": "W",
            "uPv1": 400,
            "uPv1Str": "V",
            "iPv1": 8,
            "iPv1Str": "A",
            "pow1": 3200,
            "pow1Str": "W",
            "uPv2": 390,
            "uPv2Str": "V",
            "iPv2": 7.5,
            "iPv2Str": "A",
            "pow2": 2925,
            "pow2Str": "W",
            "batteryPower": 1.2,
            "batteryPowerStr": "kW",
            "batteryPowerPec": "1",
            "storageBatteryCurrent": -4.0,
            "storageBatteryCurrentStr": "A",
            "storageBatteryVoltage": 51.2,
            "storageBatteryVoltageStr": "V",
            "batteryCapacitySoc": 72,
            "batteryHealthSoh": 96,
            "batteryTodayChargeEnergy": 2.5,
            "batteryTodayChargeEnergyStr": "kWh",
            "batteryTodayDischargeEnergy": 1.25,
            "batteryTodayDischargeEnergyStr": "kWh",
            "batteryTotalChargeEnergy": 800,
            "batteryTotalChargeEnergyStr": "kWh",
            "batteryTotalDischargeEnergy": 790,
            "batteryTotalDischargeEnergyStr": "kWh",
            "socChargingSet": 20,
            "socDischargeSet": 15,
            "psum": -0.5,
            "psumStr": "kW",
            "familyLoadPower": 1.1,
            "familyLoadPowerStr": "kW",
            "hmiVersionAll": "4B0100",
        }
    )
    grid = _common(GRID_SERIAL, "2003", BARN, "Barn Grid")
    grid.update(
        {
            "productModel": "1",
            "series": "S5-GR3P10K",
            "machine": "S5-GR3P10K",
            "state": 2,
            "acOutputType": 1,
            "dcInputType": 0,
            "pac": 1500,
            "pacStr": "W",
            "eToday": 0.5,
            "eTodayStr": "kWh",
            "eTotal": 3.2,
            "eTotalStr": "MWh",
            "uAc1": 230,
            "uAc1Str": "V",
            "uAc2": 231,
            "uAc2Str": "V",
            "uAc3": 229,
            "uAc3Str": "V",
            "iAc1": 0,
            "iAc1Str": "A",
            "iAc2": 0,
            "iAc2Str": "A",
            "iAc3": 0,
            "iAc3Str": "A",
            "uPv1": 20,
            "uPv1Str": "V",
            "iPv1": 0,
            "iPv1Str": "A",
            "pow1": 0,
            "pow1Str": "W",
            "psum": 0,
            "psumStr": "kW",
            "familyLoadPower": 0.2,
            "familyLoadPowerStr": "kW",
        }
    )
    shed = _common(STALE_SERIAL, "2004", NORTH, "South Shed")
    shed.update(
        {
            "productModel": "1",
            "series": "S6-GR1P3K",
            "machine": "S6-GR1P3K",
            "state": 1,
            "acOutputType": 0,
            "dcInputType": 0,
            "pac": 2,
            "pacStr": "kW",
            "eToday": 1,
            "eTodayStr": "kWh",
            "eTotal": 100,
            "eTotalStr": "kWh",
            "uAc1": 230,
            "uAc1Str": "V",
            "iAc1": 8,
            "iAc1Str": "A",
            "uPv1": 300,
            "uPv1Str": "V",
            "iPv1": 6,
            "iPv1Str": "A",
            "pow1": 1800,
            "pow1Str": "W",
            "stale": True,
        }
    )
    return {
        NORTH_SERIAL: north,
        HYBRID_SERIAL: hybrid,
        GRID_SERIAL: grid,
        STALE_SERIAL: shed,
    }


def alarms() -> list[dict[str, Any]]:
    """Return one pending alarm and one restored alarm."""
    return [
        {
            "id": "9001",
            "stationId": BARN,
            "alarmDeviceSn": HYBRID_SERIAL,
            "alarmCode": "2129",
            "alarmLevel": "2",
            "alarmMsg": "DC arc fault",
            "advice": "Inspect the PV connectors",
            "state": "0",
            "alarmBeginTime": 1_700_000_000_000,
        },
        {
            "id": "9000",
            "stationId": NORTH,
            "alarmDeviceSn": NORTH_SERIAL,
            "alarmCode": "1015",
            "alarmLevel": "1",
            "alarmMsg": "No grid",
            "advice": "",
            "state": "2",
            "alarmBeginTime": 1_600_000_000_000,
        },
    ]


def collectors() -> list[dict[str, Any]]:
    """Return one stick per plant."""
    return [
        {
            "sn": "COLLECTOR1001",
            "stationId": NORTH,
            "model": "S2-WL-ST",
            "state": 1,
            "rssi": -63,
        },
        {
            "sn": "COLLECTOR1002",
            "stationId": BARN,
            "model": "S3-WIFI-ST",
            "state": 1,
            "rssi": -70,
        },
    ]


def epms() -> list[dict[str, Any]]:
    """Return one export power manager."""
    return [
        {
            "id": "3001",
            "sn": "EPM1002",
            "stationId": BARN,
            "state": 1,
            "pEpmTotal": 0.8,
            "pEpmTotalStr": "kW",
        }
    ]


def null_detail() -> dict[str, Any]:
    """A detail body full of nulls, used by the nulls scenario."""
    detail = _common(NORTH_SERIAL, "2001", NORTH, "North Roof")
    detail.update(
        {
            "state": 1,
            "acOutputType": 0,
            "productModel": "1",
            "series": "S6-GR1P5K",
            "machine": "S6-GR1P5K",
            "pac": None,
            "pacStr": None,
            "eToday": None,
            "eTodayStr": "kWh",
            "eTotal": None,
            "batteryPower": None,
            "batteryCapacitySoc": None,
            "uPv1": None,
            "uAc1": None,
        }
    )
    return detail
