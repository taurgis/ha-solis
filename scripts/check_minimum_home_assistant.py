#!/usr/bin/env python3
"""Boot Home Assistant against the mock SolisCloud API.

The default image is the ``homeassistant`` field in ``hacs.json``. Pass a
version argument to boot another tag, such as the current stable release.

The check fails unless the config entry loads, AC power matches the mock,
a short 502 keeps that power, a sustained 502 hides it and opens a repair
that clears on recovery, one inverter can fail on its own, bad bodies do not
start reauthentication, and a null, zero, reset or spike does not move the
lifetime total backwards. It then starts the Solarman stick emulator, adds a
local entry, checks the captured string map, sleeps the stick, and brings it
back on 127.0.0.2. A second entry with cloud fallback must keep the local
lifetime total.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.error
from urllib.parse import urlencode
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ghcr.io/home-assistant/home-assistant"
CORE_NAME = "ha-solis-check-core"
MOCK_PORT = 8765
HOST_PORT = 8123
USERNAME = "admin"
PASSWORD = "solis-dev-pass"
BOOT_TIMEOUT_SECONDS = 240
API_URL = f"http://127.0.0.1:{MOCK_PORT}"
NORTH = "1900000000000001001"
BARN = "1900000000000001002"
STICK_CONTROL = "http://127.0.0.1:8877"
LOGGER_SERIAL = "1902137671"
LOCAL_TOTAL = 21970.0
MORNING_HOST = "127.0.0.2"


class CheckError(Exception):
    """The lab boot did not produce a working Solis entry."""


def _docker() -> list[str]:
    probe = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    if probe.returncode == 0:
        return ["docker"]
    return ["sudo", "docker"]


DOCKER = _docker()


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, text=True, capture_output=True)


def _log(message: str) -> None:
    print(message, flush=True)


def minimum_version() -> str:
    payload = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    version = payload.get("homeassistant")
    if not isinstance(version, str) or not version:
        raise CheckError("hacs.json is missing a homeassistant version")
    return version


def cleanup() -> None:
    _run([*DOCKER, "rm", "-f", CORE_NAME], check=False)


def _http(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    *,
    token: str | None = None,
    form: dict[str, str] | None = None,
    timeout: float = 180,
) -> tuple[int, Any]:
    headers: dict[str, str] = {}
    data: bytes | None = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if form is not None:
        data = urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        payload = err.read().decode()
        try:
            parsed: Any = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            parsed = payload
        return err.code, parsed


def _wait_for_onboarding(base: str) -> list[dict[str, Any]]:
    deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS
    last: Any = None
    while time.monotonic() < deadline:
        try:
            status, body = _http("GET", f"{base}/api/onboarding", timeout=10)
            api_status, _api = _http("GET", f"{base}/api/", timeout=10)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
            last = str(err)
            time.sleep(2)
            continue
        if api_status in {200, 401} and status == 200 and isinstance(body, list):
            return body
        last = {"onboarding": status, "api": api_status}
        time.sleep(2)
    raise CheckError(f"Home Assistant did not finish booting: {last}")


def _exchange(base: str, code: str) -> str:
    status, tokens = _http(
        "POST",
        f"{base}/auth/token",
        form={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": f"{base}/",
        },
    )
    if status != 200 or not isinstance(tokens, dict) or not tokens.get("access_token"):
        raise CheckError(f"token exchange failed: {status} {tokens}")
    return str(tokens["access_token"])


def _onboard(base: str, steps: list[dict[str, Any]]) -> str:
    done = {row.get("step"): bool(row.get("done")) for row in steps}
    client_id = f"{base}/"
    status, created = _http(
        "POST",
        f"{base}/api/onboarding/users",
        {
            "client_id": client_id,
            "name": "Admin",
            "username": USERNAME,
            "password": PASSWORD,
            "language": "en",
        },
    )
    if status != 200 or not isinstance(created, dict) or not created.get("auth_code"):
        raise CheckError(f"onboarding user failed: {status} {created}")
    token = _exchange(base, str(created["auth_code"]))
    follow_ups = (
        ("core_config", "/api/onboarding/core_config", {}),
        ("analytics", "/api/onboarding/analytics", {}),
        (
            "integration",
            "/api/onboarding/integration",
            {"client_id": client_id, "redirect_uri": f"{base}/?auth_callback=1"},
        ),
    )
    for step, path, payload in follow_ups:
        if done.get(step):
            continue
        step_status, step_body = _http("POST", f"{base}{path}", payload, token=token)
        if step_status not in {200, 204}:
            raise CheckError(f"onboarding {step} failed: {step_status} {step_body}")
    return token


def _advance(base: str, token: str, flow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    status, body = _http(
        "POST",
        f"{base}/api/config/config_entries/flow/{flow_id}",
        payload,
        token=token,
    )
    if status != 200 or not isinstance(body, dict):
        raise CheckError(f"config flow step failed: {status} {body}")
    return body


def _add_solis(base: str, token: str) -> str:
    status, started = _http(
        "POST",
        f"{base}/api/config/config_entries/flow",
        {"handler": "solis", "show_advanced_options": False},
        token=token,
    )
    if status != 200 or not isinstance(started, dict):
        raise CheckError(f"config flow did not start: {status} {started}")
    flow_id = str(started.get("flow_id") or "")
    current = _advance(
        base,
        token,
        flow_id,
        {
            "api_url": API_URL,
            "key_id": "test-key",
            "key_secret": "test-secret",
        },
    )
    if current.get("type") != "form" or current.get("step_id") != "stations":
        raise CheckError(f"expected the plant step: {json.dumps(current)[:1500]}")
    current = _advance(
        base,
        token,
        flow_id,
        {"station_ids": [NORTH, BARN]},
    )
    if current.get("type") != "create_entry":
        raise CheckError(f"config flow did not create an entry: {json.dumps(current)[:1500]}")
    result = current.get("result")
    if isinstance(result, dict) and result.get("entry_id"):
        return str(result["entry_id"])
    raise CheckError(f"create_entry had no entry id: {json.dumps(current)[:1500]}")


def _states(base: str, token: str) -> list[dict[str, Any]]:
    status, states = _http("GET", f"{base}/api/states", token=token)
    if status != 200 or not isinstance(states, list):
        raise CheckError(f"could not read states: {status}")
    return [state for state in states if isinstance(state, dict)]


def _find(states: list[dict[str, Any]], suffix: str) -> dict[str, Any]:
    matches = [state for state in states if str(state.get("entity_id", "")).endswith(suffix)]
    if len(matches) != 1:
        ids = [state.get("entity_id") for state in states if "solis" in str(state.get("entity_id"))]
        raise CheckError(f"expected one entity ending {suffix}, found {matches or ids}")
    return matches[0]


def _number(state: dict[str, Any]) -> float | None:
    raw = state.get("state")
    if raw in {None, "unknown", "unavailable"}:
        return None
    try:
        return float(str(raw))
    except ValueError:
        return None


def _refresh(base: str, token: str, entity_id: str) -> None:
    status, body = _http(
        "POST",
        f"{base}/api/services/homeassistant/update_entity",
        {"entity_id": entity_id},
        token=token,
    )
    if status not in {200, 204}:
        raise CheckError(f"update_entity failed: {status} {body}")


def _wait_state(
    base: str,
    token: str,
    entity_id: str,
    predicate: Any,
    timeout: float = 60,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        for state in _states(base, token):
            if state.get("entity_id") != entity_id:
                continue
            last = state.get("state")
            if predicate(state):
                return state
        time.sleep(1)
    raise CheckError(f"{entity_id} did not reach the expected state; last {last}")


def _ws(base: str, token: str, message: dict[str, Any]) -> Any:
    import websockets

    async def _call() -> Any:
        url = base.replace("http://", "ws://") + "/api/websocket"
        async with websockets.connect(url) as socket:
            await socket.recv()
            await socket.send(json.dumps({"type": "auth", "access_token": token}))
            auth = json.loads(await socket.recv())
            if auth.get("type") != "auth_ok":
                raise CheckError(f"websocket auth failed: {auth}")
            await socket.send(json.dumps({"id": 1, **message}))
            while True:
                payload = json.loads(await socket.recv())
                if payload.get("id") != 1:
                    continue
                if not payload.get("success"):
                    raise CheckError(f"websocket {message['type']} failed: {payload}")
                return payload.get("result")

    return asyncio.run(_call())


def _reconfigure(base: str, token: str, entry_id: str) -> None:
    status, started = _http(
        "POST",
        f"{base}/api/config/config_entries/flow",
        {"handler": "solis", "show_advanced_options": False, "entry_id": entry_id},
        token=token,
    )
    if status != 200 or not isinstance(started, dict):
        raise CheckError(f"reconfigure did not start: {status} {started}")
    current = _advance(
        base,
        token,
        str(started.get("flow_id") or ""),
        {
            "api_url": API_URL,
            "station_ids": [NORTH, BARN],
            "scan_interval": 300,
            "request_timeout": 30,
            "endpoint_interval": 2,
            "time_zone": "plant",
        },
    )
    if current.get("type") != "abort" or current.get("reason") != "reconfigure_successful":
        raise CheckError(f"reconfigure failed: {json.dumps(current)[:1500]}")


def _finish_reauth(base: str, token: str) -> None:
    progress = _ws(base, token, {"type": "config_entries/flow/progress"})
    if not isinstance(progress, list):
        raise CheckError(f"reauth progress was not a list: {progress}")
    flow_id = ""
    for flow in progress:
        if isinstance(flow, dict) and flow.get("handler") == "solis":
            flow_id = str(flow.get("flow_id") or "")
            break
    if not flow_id:
        raise CheckError(f"reauth flow was not in progress: {progress}")
    current = _advance(
        base,
        token,
        flow_id,
        {"key_id": "test-key", "key_secret": "test-secret"},
    )
    if current.get("type") != "abort" or current.get("reason") != "reauth_successful":
        raise CheckError(f"reauth did not finish: {json.dumps(current)[:1500]}")


def _version_at_least(version: str, major: int, minor: int) -> bool:
    numbers = [int(part) for part in version.split(".")[:2]]
    return (numbers[0], numbers[1]) >= (major, minor)


def _assert_local_brand(base: str, token: str) -> None:
    """Home Assistant 2026.3+ serves custom_components/solis/brand locally."""
    icon = (ROOT / "custom_components" / "solis" / "brand" / "icon.png").read_bytes()
    request = urllib.request.Request(
        f"{base}/api/brands/integration/solis/icon.png",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        status = response.status
    if status != 200 or body != icon:
        raise CheckError(
            f"local brand icon was not served: status {status}, "
            f"{len(body)} bytes, expected {len(icon)}"
        )
    _log("Home Assistant served the local Solis brand icon")


def _issues(base: str, token: str) -> list[dict[str, Any]]:
    import websockets

    async def _call() -> list[dict[str, Any]]:
        url = base.replace("http://", "ws://") + "/api/websocket"
        async with websockets.connect(url) as socket:
            await socket.recv()
            await socket.send(json.dumps({"type": "auth", "access_token": token}))
            auth = json.loads(await socket.recv())
            if auth.get("type") != "auth_ok":
                raise CheckError(f"websocket auth failed: {auth}")
            await socket.send(json.dumps({"id": 1, "type": "repairs/list_issues"}))
            while True:
                message = json.loads(await socket.recv())
                if message.get("id") != 1:
                    continue
                if not message.get("success"):
                    raise CheckError(f"repairs list failed: {message}")
                result = message.get("result")
                if isinstance(result, dict) and isinstance(result.get("issues"), list):
                    return result["issues"]
                raise CheckError(f"unexpected repairs payload: {message}")

    return asyncio.run(_call())


def _require_energy(base: str, token: str, entity_id: str, held: float) -> None:
    """Fail if the lifetime total moved off the last good mock value."""
    matched = _wait_state(
        base,
        token,
        entity_id,
        lambda item: _number(item) == held,
    )
    if _number(matched) != held:
        raise CheckError(f"lifetime energy moved: {matched}")


def _cloud_faults(
    base: str,
    token: str,
    power_id: str,
    total_id: str,
    battery_id: str,
    held: float,
    north_power: float,
    hybrid_battery: float,
) -> None:
    """Exercise the mock fault modes against a running Home Assistant."""
    _mock_control({"fault": "outage"})
    _refresh(base, token, power_id)
    _wait_state(base, token, power_id, lambda item: _number(item) == north_power, timeout=150)
    _refresh(base, token, power_id)
    _wait_state(base, token, power_id, lambda item: _number(item) == north_power, timeout=150)
    _refresh(base, token, power_id)
    _wait_state(
        base,
        token,
        power_id,
        lambda item: item.get("state") == "unavailable",
        timeout=150,
    )
    _require_energy(base, token, total_id, held)
    if "cloud_outage" not in json.dumps(_issues(base, token)):
        raise CheckError("sustained outage did not open a repair")
    _log("Sustained 502 hid live power, held energy, and opened a repair")
    _mock_control({"fault": "ok"})
    _refresh(base, token, power_id)
    _wait_state(base, token, power_id, lambda item: _number(item) == north_power, timeout=150)
    _require_energy(base, token, total_id, held)
    if "cloud_outage" in json.dumps(_issues(base, token)):
        raise CheckError("outage repair was still open after recovery")
    _log("Power recovered to the mock value and the repair cleared")
    _mock_control({"fault": "partial"})
    _refresh(base, token, power_id)
    _wait_state(
        base,
        token,
        power_id,
        lambda item: item.get("state") == "unavailable",
        timeout=150,
    )
    _wait_state(
        base,
        token,
        battery_id,
        lambda item: _number(item) == hybrid_battery,
        timeout=150,
    )
    _require_energy(base, token, total_id, held)
    _log("One inverter failed while the other kept its mock battery power")
    _mock_control({"fault": "ok"})
    _refresh(base, token, power_id)
    _wait_state(base, token, power_id, lambda item: _number(item) == north_power, timeout=150)
    for fault in ("garbage", "html", "partial_json", "unavailable", "rate_limit"):
        _mock_control({"fault": fault})
        _refresh(base, token, power_id)
        _wait_state(
            base,
            token,
            power_id,
            lambda item: _number(item) == north_power,
            timeout=150,
        )
        _require_energy(base, token, total_id, held)
        if "invalid_auth" in json.dumps(_issues(base, token)):
            raise CheckError(f"{fault} started reauthentication")
        _mock_control({"fault": "ok"})
        _refresh(base, token, power_id)
        _wait_state(
            base,
            token,
            power_id,
            lambda item: _number(item) == north_power,
            timeout=150,
        )
    _log("Garbage, HTML, 503 and 429 kept the last good values")
    _mock_control({"fault": "stale"})
    _refresh(base, token, power_id)
    _wait_state(base, token, power_id, lambda item: _number(item) == north_power, timeout=150)
    _require_energy(base, token, total_id, held)
    _mock_control({"fault": "ok"})
    _refresh(base, token, power_id)
    _wait_state(base, token, power_id, lambda item: _number(item) == north_power, timeout=150)
    _mock_control({"fault": "spike"})
    _refresh(base, token, total_id)
    _require_energy(base, token, total_id, held)
    _mock_control({"fault": "reset"})
    _refresh(base, token, total_id)
    _require_energy(base, token, total_id, held)
    _log(f"Lifetime energy stayed at {held} through stale, spike and reset samples")
    _mock_control({"fault": "ok"})
    _mock_control({"fault": "slow", "slow_seconds": 0.2})
    _refresh(base, token, power_id)
    _wait_state(base, token, power_id, lambda item: _number(item) == north_power, timeout=150)
    _mock_control({"fault": "flap"})
    _refresh(base, token, power_id)
    _wait_state(base, token, power_id, lambda item: _number(item) == north_power, timeout=180)
    _require_energy(base, token, total_id, held)
    _mock_control({"fault": "ok"})
    _refresh(base, token, power_id)
    _wait_state(base, token, power_id, lambda item: _number(item) == north_power, timeout=150)
    _log("Slow and flapping responses still matched the mock")


def _ensure_loopback_alias() -> None:
    """Give the morning stick an address pytest-socket would have blocked."""
    shown = subprocess.run(["ip", "addr", "show", "dev", "lo"], capture_output=True, text=True)
    if MORNING_HOST in shown.stdout:
        return
    added = subprocess.run(
        ["sudo", "ip", "addr", "add", f"{MORNING_HOST}/8", "dev", "lo"],
        capture_output=True,
        text=True,
        check=False,
    )
    if added.returncode != 0 and "File exists" not in added.stderr:
        raise CheckError(f"could not add {MORNING_HOST}: {added.stderr}")


def _start_stick() -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT}{os.pathsep}{ROOT / 'tools'}"
    _log("Starting the Solarman stick emulator")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mock_soliscloud.stick",
            "--host",
            "127.0.0.1",
            "--control-port",
            "8877",
        ],
        cwd=ROOT / "tools",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            status, body = _http("POST", f"{STICK_CONTROL}/__control", {}, timeout=2)
        except (urllib.error.URLError, TimeoutError, OSError):
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                raise CheckError(f"stick emulator exited: {output}") from None
            time.sleep(0.3)
            continue
        if status == 200 and isinstance(body, dict) and body.get("serial") == int(LOGGER_SERIAL):
            return process
        time.sleep(0.3)
    raise CheckError("stick emulator did not open its control port")


def _stick_control(payload: dict[str, Any]) -> None:
    status, body = _http("POST", f"{STICK_CONTROL}/__control", payload, timeout=10)
    if status != 200:
        raise CheckError(f"stick control failed: {status} {body}")


def _config_entry(base: str, token: str, entry_id: str) -> dict[str, Any]:
    status, entries = _http("GET", f"{base}/api/config/config_entries/entry", token=token)
    if status != 200 or not isinstance(entries, list):
        raise CheckError(f"could not list config entries: {status}")
    for entry in entries:
        if isinstance(entry, dict) and entry.get("entry_id") == entry_id:
            return entry
    raise CheckError(f"config entry {entry_id} is gone")


def _wait_loaded(base: str, token: str, entry_id: str, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    state: Any = None
    while time.monotonic() < deadline:
        try:
            state = _config_entry(base, token, entry_id).get("state")
        except CheckError:
            time.sleep(1)
            continue
        if state == "loaded":
            return
        if state == "setup_error":
            raise CheckError(f"config entry setup_error: {entry_id}")
        time.sleep(1)
    raise CheckError(f"config entry did not load: {state}")


def _stored_logger_host(config: Path, entry_id: str) -> str | None:
    """Read the address from Home Assistant's storage. The REST list omits entry data."""
    path = config / ".storage" / "core.config_entries"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    data = payload.get("data")
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("entry_id") == entry_id:
            stored = entry.get("data")
            if isinstance(stored, dict):
                host = stored.get("logger_host")
                return str(host) if host else None
    return None


def _wait_stored_host(config: Path, entry_id: str, host: str) -> None:
    deadline = time.monotonic() + 30
    last: str | None = None
    while time.monotonic() < deadline:
        last = _stored_logger_host(config, entry_id)
        if last == host:
            return
        time.sleep(1)
    raise CheckError(f"stored logger host stayed {last}, expected {host}")


def _registry(base: str, token: str) -> list[dict[str, Any]]:
    result = _ws(base, token, {"type": "config/entity_registry/list"})
    rows: Any = result
    if isinstance(result, dict):
        rows = result.get("entities")
    if not isinstance(rows, list):
        raise CheckError(f"entity registry was not a list: {type(result).__name__}")
    return [row for row in rows if isinstance(row, dict)]


def _entity_id(base: str, token: str, unique_id: str) -> str:
    for row in _registry(base, token):
        if row.get("unique_id") == unique_id and row.get("platform") == "solis":
            return str(row["entity_id"])
    raise CheckError(f"no Solis entity with unique id {unique_id}")


def _require_unique(
    base: str,
    token: str,
    unique_id: str,
    predicate: Any,
    timeout: float = 60,
) -> dict[str, Any]:
    entity_id = ""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            entity_id = _entity_id(base, token, unique_id)
        except CheckError as err:
            last = str(err)
            time.sleep(1)
            continue
        for state in _states(base, token):
            if state.get("entity_id") != entity_id:
                continue
            last = state.get("state")
            if predicate(state):
                return state
        time.sleep(1)
    raise CheckError(f"{unique_id} ({entity_id}) did not match; last {last}")


def _refresh_until(base: str, token: str, entity_id: str) -> None:
    deadline = time.monotonic() + 30
    last: Any = None
    while time.monotonic() < deadline:
        status, body = _http(
            "POST",
            f"{base}/api/services/homeassistant/update_entity",
            {"entity_id": entity_id},
            token=token,
        )
        if status in {200, 204}:
            return
        last = body
        time.sleep(1)
    raise CheckError(f"update_entity failed: {last}")


def _add_logger(base: str, token: str, source: str, host: str) -> str:
    status, started = _http(
        "POST",
        f"{base}/api/config/config_entries/flow",
        {"handler": "solis", "show_advanced_options": False},
        token=token,
    )
    if status != 200 or not isinstance(started, dict):
        raise CheckError(f"logger flow did not start: {status} {started}")
    flow_id = str(started.get("flow_id") or "")
    user: dict[str, Any] = {"api_url": API_URL, "source": source}
    if source != "local":
        user["key_id"] = "test-key"
        user["key_secret"] = "test-secret"
    current = _advance(base, token, flow_id, user)
    if source == "both":
        if current.get("step_id") != "stations":
            raise CheckError(f"expected the plant step: {json.dumps(current)[:1500]}")
        current = _advance(base, token, flow_id, {"station_ids": [NORTH, BARN]})
    if current.get("step_id") != "logger":
        raise CheckError(f"expected the logger step: {json.dumps(current)[:1500]}")
    current = _advance(
        base,
        token,
        flow_id,
        {"logger_kind": "auto", "logger_host": host, "logger_serial": LOGGER_SERIAL},
    )
    if current.get("type") != "create_entry":
        raise CheckError(f"logger flow did not create an entry: {json.dumps(current)[:1500]}")
    result = current.get("result")
    if isinstance(result, dict) and result.get("entry_id"):
        return str(result["entry_id"])
    raise CheckError(f"logger create_entry had no entry id: {json.dumps(current)[:1500]}")


def _delete_entry(base: str, token: str, entry_id: str) -> None:
    status, body = _http(
        "DELETE",
        f"{base}/api/config/config_entries/entry/{entry_id}",
        token=token,
    )
    if status not in {200, 204}:
        raise CheckError(f"could not remove the local entry: {status} {body}")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status, entries = _http("GET", f"{base}/api/config/config_entries/entry", token=token)
        gone = (
            status == 200
            and isinstance(entries, list)
            and not any(
                isinstance(entry, dict) and entry.get("entry_id") == entry_id for entry in entries
            )
        )
        if gone:
            return
        time.sleep(1)
    raise CheckError(f"config entry {entry_id} was not removed")


def _require_no_cloud_outage(base: str, token: str) -> None:
    blob = json.dumps(_issues(base, token))
    if "cloud_outage" in blob:
        raise CheckError(f"asleep logger opened a cloud outage repair: {blob[:1500]}")


def _exercise_local(base: str, token: str, config: Path) -> None:
    """Local setup, night, a new DHCP address, then cloud totals that must not win."""
    _stick_control({"registers": {"3005": 500}})
    entry_id = _add_logger(base, token, "local", "127.0.0.1")
    _wait_loaded(base, token, entry_id)
    _require_unique(base, token, "SOLIS001_energy_total", lambda item: _number(item) == LOCAL_TOTAL)
    _require_unique(base, token, "SOLIS001_data_source", lambda item: item.get("state") == "local")
    _require_unique(base, token, "SOLIS001_ac_power", lambda item: _number(item) == 500)
    _require_unique(base, token, "SOLIS001_phase_1_voltage", lambda item: _number(item) == 234.9)
    _log("Local logger published 21970 kWh, 500 W and 234.9 V")
    energy_id = _entity_id(base, token, "SOLIS001_energy_total")
    _stick_control({"night": True})
    _refresh_until(base, token, energy_id)
    _require_unique(base, token, "SOLIS001_ac_power", lambda item: _number(item) == 0)
    _require_unique(base, token, "SOLIS001_energy_total", lambda item: _number(item) == LOCAL_TOTAL)
    _require_unique(base, token, "SOLIS001_data_source", lambda item: item.get("state") == "asleep")
    _require_no_cloud_outage(base, token)
    _log("Night closed the stick: power 0, lifetime energy held, source asleep")
    _stick_control({"night": False, "host": MORNING_HOST, "registers": {"3005": 500}})
    _refresh_until(base, token, energy_id)
    _require_unique(base, token, "SOLIS001_data_source", lambda item: item.get("state") == "local")
    _require_unique(base, token, "SOLIS001_energy_total", lambda item: _number(item) == LOCAL_TOTAL)
    _require_unique(base, token, "SOLIS001_ac_power", lambda item: _number(item) == 500)
    _wait_stored_host(config, entry_id, MORNING_HOST)
    _wait_loaded(base, token, entry_id)
    status, body = _http(
        "POST",
        f"{base}/api/services/homeassistant/reload_config_entry",
        {"entry_id": entry_id},
        token=token,
    )
    if status not in {200, 204}:
        raise CheckError(f"reload failed: {status} {body}")
    _wait_loaded(base, token, entry_id)
    _refresh_until(base, token, energy_id)
    _require_unique(base, token, "SOLIS001_data_source", lambda item: item.get("state") == "local")
    _require_unique(base, token, "SOLIS001_ac_power", lambda item: _number(item) == 500)
    logs = _run([*DOCKER, "logs", CORE_NAME], check=False)
    combined = f"{logs.stdout}\n{logs.stderr}"
    moved = combined.count(f"Solarman logger {LOGGER_SERIAL} is now at {MORNING_HOST}")
    if moved != 1:
        raise CheckError(f"expected one IP-change log line, found {moved}")
    _log(f"Logger {LOGGER_SERIAL} moved to {MORNING_HOST} and the entry was updated once")
    _delete_entry(base, token, entry_id)
    both_id = _add_logger(base, token, "both", MORNING_HOST)
    _wait_loaded(base, token, both_id)
    _require_unique(base, token, "SOLIS001_energy_total", lambda item: _number(item) == LOCAL_TOTAL)
    _require_unique(base, token, "SOLIS001_data_source", lambda item: item.get("state") == "local")
    _log("Local plus cloud kept the stick total of 21970 kWh")
    energy_id = _entity_id(base, token, "SOLIS001_energy_total")
    _stick_control({"night": True})
    _refresh_until(base, token, energy_id)
    _require_unique(base, token, "SOLIS001_ac_power", lambda item: _number(item) == 0)
    _require_unique(base, token, "SOLIS001_energy_total", lambda item: _number(item) == LOCAL_TOTAL)
    _require_unique(base, token, "SOLIS001_data_source", lambda item: item.get("state") == "asleep")
    _require_no_cloud_outage(base, token)
    expected = _expected()
    north = next(row for row in expected["inverters"] if row["serial"].endswith("0001"))
    cloud_power = _find(_states(base, token), "s6_gr1p5k_ac_power")
    if _number(cloud_power) != north["ac_power_w"]:
        raise CheckError(f"cloud entry changed while the stick slept: {cloud_power}")
    _stick_control({"night": False, "host": MORNING_HOST})
    _log("Night with cloud configured still published the asleep local snapshot")


def _mock_control(payload: dict[str, Any]) -> None:
    status, body = _http("POST", f"{API_URL}/__control", payload, timeout=10)
    if status != 200:
        raise CheckError(f"mock control failed: {status} {body}")


def _expected() -> dict[str, Any]:
    status, body = _http("GET", f"{API_URL}/__expected", timeout=10)
    if status != 200 or not isinstance(body, dict):
        raise CheckError(f"mock expected payload failed: {status} {body}")
    return body


def _ignore_bytecode(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name == "__pycache__" or name.endswith(".pyc")}


def _config_dir() -> Path:
    config = Path(tempfile.mkdtemp(prefix="ha-solis-"))
    destination = config / "custom_components" / "solis"
    shutil.copytree(
        ROOT / "custom_components" / "solis",
        destination,
        ignore=_ignore_bytecode,
    )
    (config / "configuration.yaml").write_text(
        "logger:\n  default: warning\n  logs:\n    custom_components.solis: info\n",
        encoding="utf-8",
    )
    return config


def _start_core(version: str, config: Path) -> None:
    _log(f"Pulling {IMAGE}:{version}")
    _run([*DOCKER, "pull", f"{IMAGE}:{version}"])
    _log(f"Starting Home Assistant {version}")
    _run(
        [
            *DOCKER,
            "run",
            "-d",
            "--name",
            CORE_NAME,
            "--network",
            "host",
            "-v",
            f"{config}:/config",
            f"{IMAGE}:{version}",
        ]
    )


def _dump_logs() -> None:
    result = _run([*DOCKER, "logs", "--tail", "160", CORE_NAME], check=False)
    print(result.stdout, file=sys.stderr)
    print(result.stderr, file=sys.stderr)


def _start_mock() -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT}{os.pathsep}{ROOT / 'tools'}"
    _log("Starting the mock SolisCloud API")
    process = subprocess.Popen(
        [sys.executable, "-m", "mock_soliscloud", "--host", "127.0.0.1", "--port", str(MOCK_PORT)],
        cwd=ROOT / "tools",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            status, body = _http("GET", f"{API_URL}/health", timeout=2)
        except (urllib.error.URLError, TimeoutError, OSError):
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                raise CheckError(f"mock exited: {output}") from None
            time.sleep(0.3)
            continue
        if status == 200 and isinstance(body, dict) and body.get("ok"):
            return process
        time.sleep(0.3)
    raise CheckError("mock SolisCloud did not become healthy")


def _assert_cloud_snapshot(base: str, token: str) -> tuple[dict[str, Any], dict[str, Any], float]:
    """Return the north power row, the lifetime sensor, and the hybrid battery watts."""
    expected = _expected()
    north = next(row for row in expected["inverters"] if str(row["serial"]).endswith("0001"))
    hybrid = next(row for row in expected["inverters"] if str(row["serial"]).endswith("0002"))
    grid = next(row for row in expected["inverters"] if str(row["serial"]).endswith("0003"))
    states = _states(base, token)
    power = _find(states, "s6_gr1p5k_ac_power")
    if _number(power) != north["ac_power_w"]:
        raise CheckError(f"AC power {power} != mock {north['ac_power_w']}")
    total = _find(states, "s6_gr1p5k_energy_total")
    if _number(total) != north["energy_total_kwh"]:
        raise CheckError(f"lifetime energy {total} != mock {north}")
    battery = _find(states, "barn_hybrid_battery_power")
    if _number(battery) != hybrid["battery_power_w"]:
        raise CheckError(f"battery power {battery} != mock {hybrid['battery_power_w']}")
    offline = _find(states, "barn_grid_ac_power")
    if _number(offline) != grid["ac_power_w"]:
        raise CheckError(f"offline power {offline} != mock {grid['ac_power_w']}")
    stale = _find(states, "south_shed_ac_power")
    if stale.get("state") != "unavailable":
        raise CheckError(f"stale inverter should be unavailable, got {stale}")
    alarm = _find(states, "barn_hybrid_alarm")
    if alarm.get("state") != "on":
        raise CheckError(f"hybrid alarm should be on, got {alarm}")
    _log(
        f"Loaded {power['entity_id']}={power['state']} "
        f"{battery['entity_id']}={battery['state']} "
        f"{offline['entity_id']}={offline['state']}"
    )
    if total["attributes"].get("state_class") != "total_increasing":
        raise CheckError(f"energy total is not total_increasing: {total['attributes']}")
    if "last_reset" in total["attributes"]:
        raise CheckError(f"energy total still publishes last_reset: {total['attributes']}")
    return power, total, float(hybrid["battery_power_w"])


def _assert_cloud_recovery(
    base: str,
    token: str,
    power_id: str,
    total_id: str,
    held: float | None,
    north_power: float,
) -> None:
    _mock_control({"energy_glitch": "null"})
    _refresh(base, token, total_id)
    _wait_state(base, token, total_id, lambda item: _number(item) == held)
    _mock_control({"energy_glitch": "zero"})
    _refresh(base, token, total_id)
    _wait_state(base, token, total_id, lambda item: _number(item) == held)
    _log(f"Lifetime energy stayed at {held} through null and zero samples")
    _mock_control({"mode": "ok", "energy_glitch": None})
    _mock_control({"mode": "auth_error"})
    _refresh(base, token, power_id)
    _wait_state(base, token, power_id, lambda item: item.get("state") == "unavailable")
    if "invalid_auth" not in json.dumps(_issues(base, token)):
        raise CheckError("reauth repair was not opened")
    _mock_control({"mode": "ok"})
    _finish_reauth(base, token)
    _refresh(base, token, power_id)
    _wait_state(base, token, power_id, lambda item: _number(item) == north_power)


def _stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def run_check(version: str) -> None:
    """Boot one Core version and exercise load, failure and recovery."""
    config = _config_dir()
    mock = _start_mock()
    stick: subprocess.Popen[str] | None = None
    try:
        _start_core(version, config)
        base = f"http://127.0.0.1:{HOST_PORT}"
        _log("Waiting for onboarding")
        token = _onboard(base, _wait_for_onboarding(base))
        _log("Creating the Solis entry against the mock")
        entry_id = _add_solis(base, token)
        _wait_loaded(base, token, entry_id, timeout=BOOT_TIMEOUT_SECONDS)
        power, total, hybrid_power = _assert_cloud_snapshot(base, token)
        _reconfigure(base, token, entry_id)
        _log("Reconfigure accepted the same plants and a plant timezone")
        if _version_at_least(version, 2026, 3):
            _assert_local_brand(base, token)
        held = _number(total)
        north_power = float(power["state"])
        _cloud_faults(
            base,
            token,
            str(power["entity_id"]),
            str(total["entity_id"]),
            str(_find(_states(base, token), "barn_hybrid_battery_power")["entity_id"]),
            held,
            north_power,
            hybrid_power,
        )
        _assert_cloud_recovery(
            base, token, str(power["entity_id"]), str(total["entity_id"]), held, north_power
        )
        _log(f"Home Assistant {version} loaded Solis, recovered from 502, and completed reauth")
        _ensure_loopback_alias()
        stick = _start_stick()
        _exercise_local(base, token, config)
        (ROOT / "lab").mkdir(exist_ok=True)
        (ROOT / "lab" / f"states-{version}.json").write_text(
            json.dumps(_states(base, token), indent=2),
            encoding="utf-8",
        )
    finally:
        _stop_process(mock)
        _stop_process(stick)
        shutil.rmtree(config, ignore_errors=True)


def main() -> None:
    version = sys.argv[1] if len(sys.argv) > 1 else minimum_version()
    cleanup()
    try:
        run_check(version)
    except CheckError as err:
        _dump_logs()
        print(err, file=sys.stderr)
        raise SystemExit(1) from err
    except Exception:
        _dump_logs()
        raise
    finally:
        cleanup()


if __name__ == "__main__":
    main()
