"""Run both sandbox services in one process.

Two uvicorn servers under one event loop, so ``python -m sandbox.serve`` is the
only thing a reviewer has to start before a run. Keeping them in-process also
means they share the :class:`~sandbox.store.SandboxStore` lock, so the ERP and
the API cannot interleave a read-modify-write on the same JSON file.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def _serve_all(erp_port: int, api_port: int, log_level: str) -> None:
    from sandbox.api.app import app as api_app
    from sandbox.erp.app import app as erp_app

    servers = [
        uvicorn.Server(
            uvicorn.Config(erp_app, host="127.0.0.1", port=erp_port, log_level=log_level)
        ),
        uvicorn.Server(
            uvicorn.Config(api_app, host="127.0.0.1", port=api_port, log_level=log_level)
        ),
    ]
    print(f"  Nexus ERP        http://127.0.0.1:{erp_port}")
    print(f"  FinanceOps API   http://127.0.0.1:{api_port}/docs")
    print("  sign in with a.rao / operator-sandbox")
    await asyncio.gather(*(server.serve() for server in servers))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the CentrAlign sandbox systems.")
    parser.add_argument("--erp-port", type=int, default=8781)
    parser.add_argument("--api-port", type=int, default=8782)
    parser.add_argument("--log-level", default="warning")
    parser.add_argument("--reset", action="store_true", help="reseed the sandbox before serving")
    args = parser.parse_args()

    if args.reset:
        from sandbox.store import STORE

        STORE.reset()
        print("sandbox reset to seed state")

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve_all(args.erp_port, args.api_port, args.log_level))


if __name__ == "__main__":
    main()
