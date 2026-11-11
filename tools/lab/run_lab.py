"""Boot Home Assistant against the dongle emulators and record each scenario.

Usage, from the repo root:
    python3.14 tools/lab/run_lab.py 2026.11.1 2025.10.0
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
REPORT = Path("/tmp/ha-solis-lab-report.json")
COMPOSE = ROOT / "tools" / "lab" / "docker-compose.yml"
CONTAINER = "ha-solis-lab-core"
SOLAR_HOST = "192.168.250.10"
MORNING_HOST = "192.168.250.15"
ADDRESSES = (
    "192.168.250.10",
    "192.168.250.11",
    "192.168.250.12",
    "192.168.250.13",
    "192.168.250.14",
    "192.168.250.15",
    "192.168.250.16",
)


def _load_check() -> Any:
    spec = importlib.util.spec_from_file_location(
        "ha_solis_check", ROOT / "scripts" / "check_minimum_home_assistant.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load the Home Assistant check helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK = _load_check()


def _log(message: str) -> None:
    print(message, flush=True)


def _run(args: list[str], *, check: bool = True, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(args, check=check, text=True, capture_output=True, env=env)
    return f"{result.stdout}\n{result.stderr}"


def _control(payload: dict[str, Any]) -> None:
    status, body = CHECK._http("POST", "http://127.0.0.1:8871/__control", payload, timeout=10)
    if status != 200 or not isinstance(body, dict) or not body.get("ok"):
        raise CHECK.CheckError(f"emulator control failed: {status} {body}")


def _prepare_network() -> None:
    """Add the lab addresses to loopback. This kernel has no dummy devices."""
    ip = "/usr/sbin/ip"
    for address in ADDRESSES:
        subprocess.run(
            ["sudo", ip, "addr", "add", f"{address}/32", "dev", "lo"],
            check=False,
            capture_output=True,
        )


def _start_emulator() -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    _log("Starting dongle emulators")
    script = str(ROOT / "tools" / "lab" / "dongles.py")
    process = subprocess.Popen(
        ["sudo", "--preserve-env=PYTHONPATH", sys.executable, script],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            status, body = CHECK._http("GET", "http://127.0.0.1:8871/__health", timeout=2)
        except (urllib.error.URLError, TimeoutError, OSError):
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                raise CHECK.CheckError(f"emulator exited: {output}") from None
            time.sleep(0.3)
            continue
        if status == 200 and isinstance(body, dict) and body.get("ok"):
            return process
        time.sleep(0.3)
    raise CHECK.CheckError("dongle emulator did not open its control port")


def _reset_devices() -> None:
    hosts = {
        "solarman": SOLAR_HOST,
        "s2": "192.168.250.11",
        "gateway": "192.168.250.12",
        "rtu": "192.168.250.13",
        "s5": "192.168.250.14",
        "blank": "192.168.250.16",
    }
    for name, host in hosts.items():
        _control({"device": name, "night": False, "mode": "ok", "host": host})


def _config_dir(source: str) -> Path:
    config = Path(tempfile.mkdtemp(prefix="ha-solis-lab-"))
    (config / "configuration.yaml").write_text(
        "default_config:\nlogger:\n  default: warning\n"
        "  logs:\n    custom_components.solis: info\n",
        encoding="utf-8",
    )
    _install(config, source)
    return config


def _install(config: Path, source: str) -> None:
    destination = config / "custom_components" / "solis"
    # Home Assistant runs as root and writes __pycache__ into the mounted tree.
    if destination.exists():
        subprocess.run(["sudo", "rm", "-rf", str(destination)], check=True)
    if source == "main":
        archive = subprocess.check_output(
            ["git", "archive", "origin/main", "custom_components/solis"], cwd=ROOT
        )
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(config)
        return
    shutil.copytree(
        ROOT / "custom_components" / "solis",
        destination,
        ignore=lambda _path, names: {name for name in names if name == "__pycache__"},
    )


def _compose(version: str, config: Path, *args: str) -> None:
    env = os.environ.copy()
    env["HA_VERSION"] = version
    env["HA_CONFIG"] = str(config)
    # sudo drops the environment, and Compose reads HA_CONFIG from it.
    command = [
        "sudo",
        "--preserve-env=HA_VERSION,HA_CONFIG",
        "docker",
        "compose",
        "-f",
        str(COMPOSE),
        *args,
    ]
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def _logs() -> str:
    return _run([*CHECK.DOCKER, "logs", CONTAINER], check=False)


def _wait_api(base: str) -> None:
    """Wait until the API answers. Onboarding is gone after the first boot."""
    deadline = time.monotonic() + 240
    last: Any = None
    while time.monotonic() < deadline:
        try:
            status, _body = CHECK._http("GET", f"{base}/api/", timeout=10)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
            last = str(err)
            time.sleep(2)
            continue
        if status in {200, 401}:
            return
        last = status
        time.sleep(2)
    raise CHECK.CheckError(f"Home Assistant did not finish booting: {last}")


def _flow(base: str, token: str, fields: dict[str, Any]) -> dict[str, Any]:
    status, started = CHECK._http(
        "POST",
        f"{base}/api/config/config_entries/flow",
        {"handler": "solis", "show_advanced_options": False},
        token=token,
    )
    if status != 200 or not isinstance(started, dict):
        raise CHECK.CheckError(f"flow did not start: {status} {started}")
    flow_id = str(started.get("flow_id") or "")
    current = CHECK._advance(
        base,
        token,
        flow_id,
        {"source": "local", "api_url": "http://127.0.0.1:9", "key_id": "", "key_secret": ""},
    )
    if current.get("step_id") != "logger":
        raise CHECK.CheckError(f"expected the logger step: {json.dumps(current)[:800]}")
    current = CHECK._advance(base, token, flow_id, fields)
    return current


def _entry_id(current: dict[str, Any]) -> str:
    if current.get("type") != "create_entry":
        raise CHECK.CheckError(f"flow did not create an entry: {json.dumps(current)[:800]}")
    result = current.get("result")
    if isinstance(result, dict) and result.get("entry_id"):
        return str(result["entry_id"])
    raise CHECK.CheckError(f"create_entry had no id: {json.dumps(current)[:800]}")


def _require(base: str, token: str, unique_id: str, predicate: Any) -> dict[str, Any]:
    return CHECK._require_unique(base, token, unique_id, predicate, timeout=90)


def _energy(base: str, token: str, serial: str) -> None:
    _require(base, token, f"{serial}_energy_total", lambda item: CHECK._number(item) == 21970)


def _refresh(base: str, token: str, serial: str) -> None:
    entity_id = CHECK._entity_id(base, token, f"{serial}_energy_total")
    CHECK._refresh_until(base, token, entity_id)


def _scenario(name: str, action: Any) -> dict[str, str]:
    try:
        action()
    except Exception as err:
        _log(f"FAIL {name}: {err}")
        return {"scenario": name, "result": "fail", "detail": str(err)}
    _log(f"PASS {name}")
    return {"scenario": name, "result": "pass", "detail": ""}


def _upgrade(base: str, token: str, config: Path, version: str) -> list[dict[str, str]]:
    """Install main, create the two old entry shapes, then boot this branch."""
    rows: list[dict[str, str]] = []

    def _create() -> None:
        solar = _entry_id(
            _flow(
                base,
                token,
                {
                    "logger_kind": "solarman",
                    "logger_host": SOLAR_HOST,
                    "logger_serial": "1902137671",
                },
            )
        )
        CHECK._wait_loaded(base, token, solar)
        _energy(base, token, "SOLIS001")
        s2 = _entry_id(
            _flow(
                base,
                token,
                {
                    "logger_kind": "s2_wl_st",
                    "logger_host": "192.168.250.11",
                    "logger_serial": "S2LOCAL1",
                },
            )
        )
        CHECK._wait_loaded(base, token, s2)
        _energy(base, token, "S2LOCAL1")
        _create.ids = (solar, s2)

    rows.append(_scenario("main_create_solarman_and_s2", _create))
    if not hasattr(_create, "ids"):
        return rows
    solar_id, s2_id = _create.ids

    def _switch() -> None:
        _compose(version, config, "stop")
        _install(config, "branch")
        _compose(version, config, "start")
        _wait_api("http://127.0.0.1:8123")
        CHECK._wait_loaded(base, token, solar_id, timeout=180)
        CHECK._wait_loaded(base, token, s2_id, timeout=180)
        _energy(base, token, "SOLIS001")
        _energy(base, token, "S2LOCAL1")
        solar_entry = CHECK._config_entry(base, token, solar_id)
        kind = str(solar_entry.get("data", {}).get("logger_kind") or "")
        if kind not in {"", "solarman"}:
            raise CHECK.CheckError(f"legacy solarman kind changed to {kind}")

    rows.append(_scenario("upgrade_branch_keeps_entities", _switch))
    return rows


def _stored_kind(config: Path, host: str) -> str | None:
    """The config-entry list API omits data. The storage file has the kind."""
    path = config / ".storage" / "core.config_entries"
    deadline = time.monotonic() + 8
    found: str | None = None
    while time.monotonic() < deadline:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            for entry in raw.get("data", {}).get("entries", []):
                data = entry.get("data") or {}
                if entry.get("domain") == "solis" and data.get("logger_host") == host:
                    kind = data.get("logger_kind")
                    if kind:
                        return str(kind)
                    found = None
        time.sleep(0.4)
    return found


def _faults(base: str, token: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    def _night() -> None:
        _control({"device": "solarman", "night": True})
        _refresh(base, token, "SOLIS001")
        _require(base, token, "SOLIS001_ac_power", lambda item: CHECK._number(item) == 0)
        _energy(base, token, "SOLIS001")
        _require(base, token, "SOLIS001_data_source", lambda item: item.get("state") == "asleep")
        blob = json.dumps(CHECK._issues(base, token))
        if "logger_" in blob:
            raise CHECK.CheckError(f"night opened a logger repair: {blob[:500]}")

    def _move() -> None:
        _control({"device": "solarman", "night": False, "host": MORNING_HOST, "mode": "ok"})
        _refresh(base, token, "SOLIS001")
        _require(base, token, "SOLIS001_data_source", lambda item: item.get("state") == "local")
        _require(base, token, "SOLIS001_ac_power", lambda item: CHECK._number(item) == 500)
        _energy(base, token, "SOLIS001")

    def _glitch() -> None:
        _control({"device": "solarman", "registers": {"3009": 0}})
        _refresh(base, token, "SOLIS001")
        _energy(base, token, "SOLIS001")
        entity_id = CHECK._entity_id(base, token, "SOLIS001_energy_total")
        status, body = CHECK._http(
            "GET",
            f"{base}/api/history/period/2026-11-11T00:00:00+00:00?filter_entity_id={entity_id}",
            token=token,
            timeout=30,
        )
        if status == 200 and isinstance(body, list):
            values = [
                CHECK._number(row)
                for bucket in body
                if isinstance(bucket, list)
                for row in bucket
                if isinstance(row, dict)
            ]
            moved = [value for value in values if value not in {None, 21970.0}]
            if moved:
                raise CHECK.CheckError(f"energy history moved: {moved}")
        _control({"device": "solarman", "registers": {"3009": 21970}})

    rows.append(_scenario("solarman_night_holds_energy", _night))
    rows.append(_scenario("solarman_address_change", _move))
    rows.append(_scenario("energy_register_zero_does_not_publish", _glitch))
    return rows


def _new_kinds(base: str, token: str, config: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    def _gateway() -> None:
        created = _flow(
            base,
            token,
            {"logger_kind": "auto", "logger_host": "192.168.250.12", "logger_serial": "GWLOCAL1"},
        )
        entry = _entry_id(created)
        CHECK._wait_loaded(base, token, entry)
        kind = _stored_kind(config, "192.168.250.12")
        if kind != "modbus_tcp":
            raise CHECK.CheckError(f"gateway page was stored as {kind}")
        _energy(base, token, "GWLOCAL1")

    def _choice() -> None:
        current = _flow(
            base,
            token,
            {"logger_kind": "auto", "logger_host": "192.168.250.16", "logger_serial": "BLANK001"},
        )
        if current.get("step_id") != "logger_choice":
            detail = json.dumps(current)[:500]
            raise CHECK.CheckError(f"expected the identification question: {detail}")
        chosen = CHECK._advance(
            base, token, str(current.get("flow_id")), {"logger_kind": "modbus_tcp"}
        )
        entry = _entry_id(chosen)
        CHECK._wait_loaded(base, token, entry)
        _energy(base, token, "BLANK001")

    def _rtu() -> None:
        entry = _entry_id(
            _flow(
                base,
                token,
                {
                    "logger_kind": "rtu_tcp",
                    "logger_host": "192.168.250.13",
                    "logger_serial": "RTULOCAL",
                    "logger_preset": "waveshare",
                },
            )
        )
        CHECK._wait_loaded(base, token, entry)
        _energy(base, token, "RTULOCAL")
        _control({"device": "rtu", "mode": "exception"})
        _refresh(base, token, "RTULOCAL")
        _energy(base, token, "RTULOCAL")
        _control({"device": "rtu", "mode": "short"})
        _refresh(base, token, "RTULOCAL")
        _energy(base, token, "RTULOCAL")
        _control({"device": "rtu", "mode": "reset"})
        _refresh(base, token, "RTULOCAL")
        _energy(base, token, "RTULOCAL")
        _control({"device": "rtu", "mode": "ok"})
        _refresh(base, token, "RTULOCAL")
        _require(base, token, "RTULOCAL_data_source", lambda item: item.get("state") == "local")

    def _s5() -> None:
        entry = _entry_id(
            _flow(
                base,
                token,
                {
                    "logger_kind": "s5_wifi_st",
                    "logger_host": "192.168.250.14",
                    "logger_serial": "S5LOCAL1",
                },
            )
        )
        CHECK._wait_loaded(base, token, entry)
        _energy(base, token, "S5LOCAL1")
        _control({"device": "s5", "mode": "closed"})
        _refresh(base, token, "S5LOCAL1")
        _energy(base, token, "S5LOCAL1")
        _require(base, token, "S5LOCAL1_ac_power", lambda item: CHECK._number(item) == 900)
        _require(base, token, "S5LOCAL1_data_source", lambda item: item.get("state") == "degraded")

    rows.append(_scenario("auto_detect_gateway_page", _gateway))
    rows.append(_scenario("identification_fallback_question", _choice))
    rows.append(_scenario("rtu_preset_and_faults_hold_energy", _rtu))
    rows.append(_scenario("s5_open_then_port_closes", _s5))
    return rows


def _restart_and_diagnostics(
    base: str, token: str, version: str, config: Path
) -> list[dict[str, str]]:
    def _restart() -> None:
        _compose(version, config, "restart")
        _wait_api(base)
        _energy(base, token, "SOLIS001")
        _energy(base, token, "S2LOCAL1")

    def _diagnostics() -> None:
        status, entries = CHECK._http("GET", f"{base}/api/config/config_entries/entry", token=token)
        if status != 200 or not isinstance(entries, list):
            raise CHECK.CheckError(f"could not list entries: {status}")
        solar = next(
            item for item in entries if isinstance(item, dict) and item.get("domain") == "solis"
        )
        entry_id = str(solar.get("entry_id") or "")
        diag_status = 0
        diag: Any = {}
        for _attempt in range(5):
            diag_status, diag = CHECK._http(
                "GET",
                f"{base}/api/diagnostics/config_entry/{entry_id}",
                token=token,
                timeout=60,
            )
            if diag_status == 200 and isinstance(diag, dict):
                break
            time.sleep(2)
        if diag_status != 200 or not isinstance(diag, dict):
            detail = str(diag)[:300]
            raise CHECK.CheckError(f"diagnostics download failed: {diag_status} {detail}")
        text = json.dumps(diag)
        if "test-secret" in text or ('key_secret": "' in text and "REDACTED" not in text):
            raise CHECK.CheckError("diagnostics leaked a credential")
        logs = _logs()
        if "test-secret" in logs:
            raise CHECK.CheckError("logs contained the API secret")

    return [
        _scenario("home_assistant_restart", _restart),
        _scenario("diagnostics_redaction", _diagnostics),
    ]


def _run_version(version: str) -> dict[str, Any]:
    _log(f"=== Home Assistant {version} ===")
    _reset_devices()
    config = _config_dir("main")
    _compose(version, config, "up", "-d")
    base = "http://127.0.0.1:8123"
    scenarios: list[dict[str, str]] = []
    try:
        token = CHECK._onboard(base, CHECK._wait_for_onboarding(base))
        scenarios.extend(_upgrade(base, token, config, version))
        if any(row["result"] != "pass" for row in scenarios):
            return {"version": version, "scenarios": scenarios}
        scenarios.extend(_faults(base, token))
        scenarios.extend(_new_kinds(base, token, config))
        scenarios.extend(_restart_and_diagnostics(base, token, version, config))
    finally:
        _compose(version, config, "down")
        shutil.rmtree(config, ignore_errors=True)
    return {"version": version, "scenarios": scenarios}


def main() -> None:
    versions = sys.argv[1:] or ["2026.11.1", "2025.10.0"]
    _prepare_network()
    emulator = _start_emulator()
    report: list[dict[str, Any]] = []
    try:
        for version in versions:
            report.append(_run_version(version))
    finally:
        emulator.terminate()
        try:
            emulator.wait(timeout=5)
        except subprocess.TimeoutExpired:
            emulator.kill()
        REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    failed = [row for version in report for row in version["scenarios"] if row["result"] != "pass"]
    _log(json.dumps(report, indent=2))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
