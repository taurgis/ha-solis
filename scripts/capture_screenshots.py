#!/usr/bin/env python3
"""Open the lab Home Assistant and save screenshots of loaded Solis entities."""

from __future__ import annotations

from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_minimum_home_assistant as lab  # noqa: E402

OUT = ROOT / "docs" / "screenshots"


def _tokens(base: str) -> dict[str, object]:
    steps = lab._wait_for_onboarding(base)
    done = {row.get("step"): bool(row.get("done")) for row in steps}
    client_id = f"{base}/"
    status, created = lab._http(
        "POST",
        f"{base}/api/onboarding/users",
        {
            "client_id": client_id,
            "name": "Admin",
            "username": lab.USERNAME,
            "password": lab.PASSWORD,
            "language": "en",
        },
    )
    if status != 200 or not isinstance(created, dict):
        raise lab.CheckError(f"onboarding user failed: {status} {created}")
    token_status, tokens = lab._http(
        "POST",
        f"{base}/auth/token",
        form={
            "grant_type": "authorization_code",
            "code": str(created["auth_code"]),
            "client_id": client_id,
        },
    )
    if token_status != 200 or not isinstance(tokens, dict):
        raise lab.CheckError(f"token exchange failed: {token_status} {tokens}")
    access = str(tokens["access_token"])
    for step, path, payload in (
        ("core_config", "/api/onboarding/core_config", {}),
        ("analytics", "/api/onboarding/analytics", {}),
        (
            "integration",
            "/api/onboarding/integration",
            {"client_id": client_id, "redirect_uri": f"{base}/?auth_callback=1"},
        ),
    ):
        if done.get(step):
            continue
        step_status, step_body = lab._http("POST", f"{base}{path}", payload, token=access)
        if step_status not in {200, 204}:
            raise lab.CheckError(f"onboarding {step} failed: {step_status} {step_body}")
    return {
        "access_token": access,
        "refresh_token": tokens.get("refresh_token", ""),
        "expires": int(time.time() * 1000) + int(tokens.get("expires_in", 1800)) * 1000,
        "hassUrl": base,
        "clientId": client_id,
    }


def main() -> None:
    version = "2026.11.1"
    OUT.mkdir(parents=True, exist_ok=True)
    config = lab._config_dir()
    mock = lab._start_mock()
    lab.cleanup()
    try:
        lab._start_core(version, config)
        base = f"http://127.0.0.1:{lab.HOST_PORT}"
        tokens = _tokens(base)
        access = str(tokens["access_token"])
        lab._add_solis(base, access)
        deadline = time.monotonic() + lab.BOOT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            states = lab._states(base, access)
            ready = any(
                str(state.get("entity_id", "")).endswith("s6_gr1p5k_ac_power") for state in states
            )
            if ready:
                power = lab._find(states, "s6_gr1p5k_ac_power")
                if lab._number(power) == 1250:
                    break
            time.sleep(2)
        else:
            raise lab.CheckError("entities were not ready for screenshots")

        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1440, "height": 1100})
            page = context.new_page()
            page.goto(base, wait_until="domcontentloaded")
            page.evaluate(
                "(tokens) => localStorage.setItem('hassTokens', JSON.stringify(tokens))",
                tokens,
            )
            page.goto(f"{base}/config/devices/dashboard", wait_until="networkidle")
            page.wait_for_timeout(2000)
            page.screenshot(path=str(OUT / "devices.png"), full_page=True)
            page.get_by_text("Barn Hybrid", exact=True).click()
            page.wait_for_timeout(2000)
            page.screenshot(path=str(OUT / "barn-hybrid.png"), full_page=True)
            page.goto(f"{base}/config/devices/dashboard", wait_until="networkidle")
            page.get_by_text("North Roof S6-GR1P5K", exact=True).click()
            page.wait_for_timeout(2000)
            page.screenshot(path=str(OUT / "north-roof.png"), full_page=True)
            browser.close()
        print(f"Wrote screenshots to {OUT}")
    finally:
        mock.terminate()
        lab.cleanup()


if __name__ == "__main__":
    try:
        main()
    except lab.CheckError as err:
        lab._dump_logs()
        print(err, file=sys.stderr)
        raise SystemExit(1) from err
