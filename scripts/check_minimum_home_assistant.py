#!/usr/bin/env python3
"""Boot Home Assistant against the mock SolisCloud API.

The default image is the ``homeassistant`` field in ``hacs.json``. Pass a
version argument to boot another tag, such as the current stable release.

The check fails unless the config entry loads, AC power matches the mock,
a 502 makes that sensor unavailable, recovery restores the value, a null or
zero lifetime total does not dip, and an auth failure opens a repair.
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


def run_check(version: str) -> None:  # noqa: PLR0915
    """Boot one Core version and exercise load, failure and recovery."""
    config = _config_dir()
    mock = _start_mock()
    try:
        _start_core(version, config)
        base = f"http://127.0.0.1:{HOST_PORT}"
        _log("Waiting for onboarding")
        token = _onboard(base, _wait_for_onboarding(base))
        _log("Creating the Solis entry against the mock")
        entry_id = _add_solis(base, token)
        deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS
        state: Any = None
        while time.monotonic() < deadline:
            status, entries = _http("GET", f"{base}/api/config/config_entries/entry", token=token)
            if status == 200 and isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("entry_id") == entry_id:
                        state = entry.get("state")
            if state == "loaded":
                break
            if state == "setup_error":
                raise CheckError(f"config entry setup_error: {entry_id}")
            time.sleep(2)
        else:
            raise CheckError(f"config entry did not load: {state}")
        expected = _expected()
        north = next(row for row in expected["inverters"] if row["serial"].endswith("0001"))
        hybrid = next(row for row in expected["inverters"] if row["serial"].endswith("0002"))
        grid = next(row for row in expected["inverters"] if row["serial"].endswith("0003"))
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
        _reconfigure(base, token, entry_id)
        _log("Reconfigure accepted the same plants and a plant timezone")
        if _version_at_least(version, 2026, 3):
            _assert_local_brand(base, token)
        if total["attributes"].get("state_class") != "total_increasing":
            raise CheckError(f"energy total is not total_increasing: {total['attributes']}")
        if "last_reset" in total["attributes"]:
            raise CheckError(f"energy total still publishes last_reset: {total['attributes']}")
        held = _number(total)
        _mock_control({"mode": "server_error"})
        _refresh(base, token, str(power["entity_id"]))
        _wait_state(
            base,
            token,
            str(power["entity_id"]),
            lambda item: item.get("state") == "unavailable",
            timeout=150,
        )
        _log("Power became unavailable after HTTP 502")
        _mock_control({"mode": "ok"})
        _refresh(base, token, str(power["entity_id"]))
        _wait_state(
            base,
            token,
            str(power["entity_id"]),
            lambda item: _number(item) == north["ac_power_w"],
        )
        _log("Power recovered to the mock value")
        _mock_control({"energy_glitch": "null"})
        _refresh(base, token, str(total["entity_id"]))
        _wait_state(
            base,
            token,
            str(total["entity_id"]),
            lambda item: _number(item) == held,
        )
        _mock_control({"energy_glitch": "zero"})
        _refresh(base, token, str(total["entity_id"]))
        _wait_state(
            base,
            token,
            str(total["entity_id"]),
            lambda item: _number(item) == held,
        )
        _log(f"Lifetime energy stayed at {held} through null and zero samples")
        _mock_control({"mode": "ok", "energy_glitch": None})
        _mock_control({"mode": "auth_error"})
        _refresh(base, token, str(power["entity_id"]))
        _wait_state(
            base,
            token,
            str(power["entity_id"]),
            lambda item: item.get("state") == "unavailable",
        )
        issue_rows = _issues(base, token)
        blob = json.dumps(issue_rows)
        if "invalid_auth" not in blob:
            raise CheckError(f"reauth repair was not opened: {blob[:1500]}")
        _mock_control({"mode": "ok"})
        _finish_reauth(base, token)
        _refresh(base, token, str(power["entity_id"]))
        _wait_state(
            base,
            token,
            str(power["entity_id"]),
            lambda item: _number(item) == north["ac_power_w"],
        )
        _log(f"Home Assistant {version} loaded Solis, recovered from 502, and completed reauth")
        (ROOT / "lab").mkdir(exist_ok=True)
        (ROOT / "lab" / f"states-{version}.json").write_text(
            json.dumps(_states(base, token), indent=2),
            encoding="utf-8",
        )
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=5)
        except subprocess.TimeoutExpired:
            mock.kill()
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
