#!/usr/bin/env python3
"""Fail unless every Solis module is above 95 percent coverage."""

from __future__ import annotations

import json
from pathlib import Path

FLOOR = 95.0
ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "coverage.json"


def main() -> None:
    """Read coverage.json and reject any integration module under the floor."""
    if not REPORT.is_file():
        raise SystemExit("coverage.json is missing; run pytest --cov-report=json first")
    payload = json.loads(REPORT.read_text(encoding="utf-8"))
    files = payload.get("files", {})
    failed = False
    for name, info in sorted(files.items()):
        normalized = name.replace("\\", "/")
        if "custom_components/solis/" not in normalized or not normalized.endswith(".py"):
            continue
        summary = info.get("summary", {})
        percent = float(summary.get("percent_covered", 0))
        if percent < FLOOR:
            print(f"{normalized}: {percent:.2f}%")
            failed = True
    if failed:
        raise SystemExit(1)
    print(f"Every Solis module is at or above {FLOOR:.0f}%")


if __name__ == "__main__":
    main()
