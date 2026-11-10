"""aiohttp SolisCloud stand-in with real HMAC-SHA1 checks.

``POST /__control`` is not part of the Solis API. Tests and the lab use it to
switch error scenarios. A Modbus or Solarman emulator should produce the same
snapshot fields, not speak this HTTP control plane.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
from typing import Any

from aiohttp import web

from .catalog import (
    KEY_ID,
    NORTH_SERIAL,
    SECRET,
    alarms,
    collectors,
    epms,
    inverter_details,
    null_detail,
    station_details,
    stations,
)
from custom_components.solis.parser import parse_inverter, parse_station
from custom_components.solis.signing import signature_matches

WINDOW = timedelta(minutes=15)


@dataclass
class MockState:
    """Mutable scenario for one mock process."""

    mode: str = "ok"
    key_id: str = KEY_ID
    secret: str = SECRET
    fail_inverter_list: bool = False
    empty_inverters: bool = False
    nulls: bool = False
    pac_scale: float = 1.0
    energy_glitch: str | None = None
    skip_serials: set[str] = field(default_factory=set)
    decoy_day: bool = False
    fail_serials: set[str] = field(default_factory=set)
    timestamp_offset_s: float = 0.0
    slow_seconds: float = 5.0
    flap_count: int = 0
    force_charge: bool = False
    calls: list[str] = field(default_factory=list)


_FAULT_MODES = {
    "ok": "ok",
    "outage": "server_error",
    "flap": "flap",
    "garbage": "garbage",
    "partial_json": "partial_json",
    "html": "html",
    "stale": "stale",
    "slow": "slow",
    "rate_limit": "rate_limit",
    "unavailable": "unavailable",
    "reset": "ok",
    "spike": "ok",
    "partial": "ok",
}


def create_app(state: MockState | None = None) -> web.Application:
    """Build the mock application."""
    app = web.Application()
    app["state"] = state or MockState()
    app.router.add_post("/v1/api/{name}", _handle)
    app.router.add_post("/__control", _control)
    app.router.add_get("/__expected", _expected)
    app.router.add_get("/health", _health)
    return app


async def _health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _control(request: web.Request) -> web.Response:
    state: MockState = request.app["state"]
    payload = await request.json()
    for key in (
        "mode",
        "fail_inverter_list",
        "empty_inverters",
        "nulls",
        "pac_scale",
        "energy_glitch",
        "decoy_day",
        "timestamp_offset_s",
        "slow_seconds",
        "force_charge",
    ):
        if key in payload:
            setattr(state, key, payload[key])
    if "skip_serials" in payload:
        skipped = payload["skip_serials"]
        state.skip_serials = set(skipped) if isinstance(skipped, list) else set()
    if "fail_serials" in payload:
        failed = payload["fail_serials"]
        state.fail_serials = set(failed) if isinstance(failed, list) else set()
    if "fault" in payload:
        _apply_fault(state, str(payload["fault"]))
    return web.json_response({"mode": state.mode, "fault": payload.get("fault")})


def _apply_fault(state: MockState, fault: str) -> None:
    """Map a lab fault name onto the mock fields that produce it."""
    state.mode = _FAULT_MODES.get(fault, state.mode)
    if fault == "spike":
        state.energy_glitch = "spike"
    elif fault == "reset":
        state.energy_glitch = "reset"
    elif fault == "partial":
        state.fail_serials.add(NORTH_SERIAL)
    elif fault == "ok":
        state.energy_glitch = None
        state.fail_serials = set()
        state.timestamp_offset_s = 0.0
        state.fail_inverter_list = False


async def _expected(request: web.Request) -> web.Response:
    """Return canonical values the integration should expose."""
    state: MockState = request.app["state"]
    now = datetime.now(UTC)
    body: dict[str, Any] = {"inverters": [], "stations": []}
    for summary in stations():
        detail = station_details()[str(summary["id"])]
        parsed = parse_station(summary, detail, None)
        body["stations"].append(
            {
                "station_id": parsed.station_id,
                "day_energy_kwh": parsed.day_energy_kwh,
                "grid_import_today_kwh": parsed.grid_import_today_kwh,
                "grid_export_today_kwh": parsed.grid_export_today_kwh,
                "load_today_kwh": parsed.load_today_kwh,
            }
        )
    for serial, detail in _details(state, now).items():
        parsed = parse_inverter(detail)
        condition = parsed.condition(now)
        power = parsed.ac_power_w
        if condition == "offline":
            power = 0.0
        if condition == "stale":
            power = None
        body["inverters"].append(
            {
                "serial": serial,
                "status": condition,
                "ac_power_w": power,
                "energy_today_kwh": parsed.energy_today_kwh,
                "energy_total_kwh": parsed.energy_total_kwh,
                "battery_power_w": None if condition == "stale" else parsed.battery_power_w,
                "battery_soc": parsed.battery_soc,
            }
        )
    return web.json_response(body)


async def _handle(request: web.Request) -> web.Response:
    state: MockState = request.app["state"]
    name = request.match_info["name"]
    path = f"/v1/api/{name}"
    body = await request.read()
    state.calls.append(path)
    if state.mode == "timeout":
        await asyncio.sleep(30)
        return web.Response(text="slow")
    if state.mode == "slow":
        await asyncio.sleep(state.slow_seconds)
    forced = _forced(state, path, body)
    if forced is not None:
        return forced
    headers = {key: request.headers[key] for key in request.headers}
    if not signature_matches(
        secret=state.secret,
        key_id=state.key_id,
        path=path,
        body=body,
        headers=headers,
    ):
        return _envelope("I0012", "Incorrect account or password, please re-enter", status=403)
    if _skewed(headers.get("Date", "")):
        return web.Response(status=408, text="Date header is outside the 15 minute window")
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return _envelope("I0000", "The necessary parameters are empty", status=200)
    if not isinstance(payload, dict):
        payload = {}
    return web.json_response(_dispatch(state, path, payload))


def _forced(state: MockState, path: str, body: bytes) -> web.Response | None:
    canned = _canned_mode(state)
    if canned is not None:
        return canned
    if state.mode == "flap":
        state.flap_count += 1
        if state.flap_count % 2 == 1:
            return web.Response(status=502, text="flap")
    if state.fail_inverter_list and path == "/v1/api/inverterList":
        return web.Response(status=502, text="inverter list down")
    if state.fail_serials and path == "/v1/api/inverterDetail":
        serial = _body_serial(body)
        if serial in state.fail_serials:
            return web.Response(status=502, text="inverter detail down")
    return None


def _canned_mode(state: MockState) -> web.Response | None:
    """Return a fixed response for modes that do not look at the request."""
    html = "<html><body>bad gateway</body></html>"
    modes = {
        "auth_error": _envelope(
            "I0012", "Incorrect account or password, please re-enter", status=403
        ),
        "clock": web.Response(status=408, text="clock"),
        "rate_limit": web.Response(status=429, text="slow down", headers={"Retry-After": "0"}),
        "server_error": web.Response(status=502, text="bad gateway"),
        "unavailable": web.Response(status=503, text="unavailable", headers={"Retry-After": "0"}),
        "html": web.Response(
            status=403, text="<html><body>nginx</body></html>", content_type="text/html"
        ),
        "garbage": web.Response(status=200, text=html, content_type="text/html"),
        "partial_json": web.Response(
            status=200, text='{"code":"0","data":', content_type="application/json"
        ),
        "abnormal": _envelope("1", "数据异常 请联系管理员", data=None),
    }
    return modes.get(state.mode)


def _body_serial(body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("sn") or "")


def _dispatch(state: MockState, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    simple = {
        "/v1/api/userStationList": lambda: _page(stations()),
        "/v1/api/alarmList": lambda: _page(alarms()),
        "/v1/api/collectorList": lambda: _page(collectors()),
        "/v1/api/epmList": lambda: _page(epms()),
    }
    if path in simple:
        return simple[path]()
    if path == "/v1/api/inverterList":
        if state.empty_inverters:
            return _page([])
        station_id = str(payload.get("stationId") or "")
        rows = []
        for detail in _details(state, now).values():
            if station_id and str(detail.get("stationId")) != station_id:
                continue
            rows.append(
                {
                    "id": detail["id"],
                    "sn": detail["sn"],
                    "stationId": detail["stationId"],
                    "name": detail["name"],
                    "state": detail.get("state"),
                    "pac": detail.get("pac"),
                    "pacStr": detail.get("pacStr"),
                }
            )
        return _page(rows)
    if path == "/v1/api/inverterDetail":
        serial = str(payload.get("sn") or "")
        detail = _details(state, now).get(serial)
        if detail is None:
            return _envelope_dict("B0049", "missing", None)
        return _envelope_dict("0", "success", detail)
    if path == "/v1/api/stationDetail":
        detail = station_details().get(str(payload.get("id")))
        return _envelope_dict("0", "success", detail)
    if path == "/v1/api/stationDayEnergyList":
        station_id = str(payload.get("stationIds") or "")
        detail = station_details().get(station_id, {})
        real = {
            "id": station_id,
            "stationId": station_id,
            "energy": detail.get("dayEnergy"),
            "energyStr": detail.get("dayEnergyStr"),
            "gridPurchasedEnergy": detail.get("gridPurchasedDayEnergy"),
            "gridPurchasedEnergyStr": detail.get("gridPurchasedDayEnergyStr"),
            "gridSellEnergy": detail.get("gridSellDayEnergy"),
            "gridSellEnergyStr": detail.get("gridSellDayEnergyStr"),
            "homeLoadEnergy": detail.get("homeLoadTodayEnergy"),
            "homeLoadEnergyStr": detail.get("homeLoadTodayEnergyStr"),
        }
        rows = [real]
        if state.decoy_day:
            rows.insert(0, {"id": "", "stationId": "", "energy": 999, "energyStr": "kWh"})
        return _page(rows)
    return _envelope_dict("I0000", f"unknown endpoint {path}", None)


def _details(state: MockState, now: datetime) -> dict[str, dict[str, Any]]:
    details = deepcopy(inverter_details())
    if state.nulls:
        details = {next(iter(details)): null_detail()}
    fresh_ms = int(now.timestamp() * 1000)
    stale_ms = int((now - timedelta(hours=2)).timestamp() * 1000)
    energy_keys = (
        "eToday",
        "eMonth",
        "eYear",
        "eTotal",
        "batteryTodayChargeEnergy",
        "batteryTodayDischargeEnergy",
        "batteryTotalChargeEnergy",
        "batteryTotalDischargeEnergy",
    )
    kept: dict[str, dict[str, Any]] = {}
    for serial, detail in details.items():
        if serial in state.skip_serials:
            continue
        if state.mode == "stale" or detail.pop("stale", False):
            detail["dataTimestamp"] = stale_ms
        elif state.timestamp_offset_s:
            shifted = now + timedelta(seconds=state.timestamp_offset_s)
            detail["dataTimestamp"] = int(shifted.timestamp() * 1000)
        else:
            detail["dataTimestamp"] = fresh_ms
        if detail.get("pac") is not None and state.pac_scale != 1:
            detail["pac"] = float(detail["pac"]) * state.pac_scale
        if state.energy_glitch == "null":
            for key in energy_keys:
                detail[key] = None
        elif state.energy_glitch == "zero":
            for key in energy_keys:
                if key in detail:
                    detail[key] = 0
        elif state.energy_glitch == "reset":
            for key in energy_keys:
                if key in detail:
                    detail[key] = 1
        elif state.energy_glitch == "spike" and "eTotal" in detail:
            detail["eTotal"] = 250000
            detail["eTotalStr"] = "kWh"
        if state.force_charge and detail.get("storageBatteryCurrent") is not None:
            detail["storageBatteryCurrent"] = abs(float(detail["storageBatteryCurrent"]))
        kept[serial] = detail
    return kept


def _page(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return _envelope_dict(
        "0",
        "success",
        {"page": {"records": rows, "total": len(rows), "pages": 1}, "records": rows},
    )


def _envelope_dict(code: str, message: str, data: Any) -> dict[str, Any]:
    return {"success": code == "0", "code": code, "msg": message, "data": data}


def _envelope(code: str, message: str, *, status: int = 200, data: Any = None) -> web.Response:
    return web.json_response(_envelope_dict(code, message, data), status=status)


def _skewed(date_header: str) -> bool:
    from email.utils import parsedate_to_datetime

    if not date_header:
        return True
    try:
        parsed = parsedate_to_datetime(date_header)
    except (TypeError, ValueError, IndexError):
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return abs(datetime.now(UTC) - parsed) > WINDOW
