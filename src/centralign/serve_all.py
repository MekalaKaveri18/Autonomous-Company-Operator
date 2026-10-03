"""Single-process entrypoint for a container host.

A deployed instance has to expose exactly one port, but the operator needs three
services: the dashboard, the mock ERP it drives with a browser, and the internal
JSON APIs. Running them as separate containers would be the textbook answer and
the wrong one here -- the ERP, the APIs and the drive are not infrastructure, they
are the *sandbox company*. They share one JSON state file and one filesystem on
purpose, so that a reset restores a byte-identical world. Splitting them across
containers would mean inventing shared storage to fake something they already
have for free.

So: three uvicorn servers on one event loop. The dashboard takes ``$PORT`` and
faces the internet; the sandbox services bind to loopback and are reachable only
from inside the container, which is also the correct security posture -- nothing
should be able to poke the mock ERP except the operator.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from pathlib import Path

import uvicorn

REPO_ROOT = Path(__file__).resolve().parents[2]
for path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)


async def _serve(port: int, erp_port: int, api_port: int, log_level: str) -> None:
    from sandbox.api.app import app as api_app
    from sandbox.erp.app import app as erp_app

    from centralign.web.app import create_app

    servers = [
        # Public: the only thing bound to 0.0.0.0.
        uvicorn.Server(
            uvicorn.Config(create_app(), host="0.0.0.0", port=port, log_level=log_level)  # noqa: S104
        ),
        uvicorn.Server(
            uvicorn.Config(erp_app, host="127.0.0.1", port=erp_port, log_level=log_level)
        ),
        uvicorn.Server(
            uvicorn.Config(api_app, host="127.0.0.1", port=api_port, log_level=log_level)
        ),
    ]
    print(f"dashboard   http://0.0.0.0:{port}", flush=True)
    print(f"sandbox erp 127.0.0.1:{erp_port} (internal)", flush=True)
    print(f"sandbox api 127.0.0.1:{api_port} (internal)", flush=True)
    await asyncio.gather(*(server.serve() for server in servers))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the operator and its sandbox in one process.")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8780")))
    parser.add_argument("--erp-port", type=int, default=8781)
    parser.add_argument("--api-port", type=int, default=8782)
    parser.add_argument("--log-level", default=os.getenv("UVICORN_LOG_LEVEL", "warning"))
    args = parser.parse_args()

    # Seed the sandbox before anything can serve a request against empty state.
    from sandbox.store import STORE

    STORE.reset()
    print("sandbox seeded", flush=True)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve(args.port, args.erp_port, args.api_port, args.log_level))


if __name__ == "__main__":
    main()
