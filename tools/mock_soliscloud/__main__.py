"""Run the mock SolisCloud server."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from mock_soliscloud.app import MockState, create_app  # noqa: E402


def main() -> None:
    """Serve until the process is stopped."""
    parser = argparse.ArgumentParser(description="Mock SolisCloud Platform API v2")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mode", default="ok")
    args = parser.parse_args()
    app = create_app(MockState(mode=args.mode))

    async def _serve() -> None:
        from aiohttp import web

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, args.host, args.port)
        await site.start()
        print(f"mock SolisCloud listening on http://{args.host}:{args.port}", flush=True)
        while True:
            await asyncio.sleep(3600)

    asyncio.run(_serve())


if __name__ == "__main__":
    main()
