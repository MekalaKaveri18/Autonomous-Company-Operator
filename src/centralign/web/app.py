"""Live dashboard: watch a run, and answer it when it asks.

Two things it has to do that a terminal cannot do as well: stream the decision
sequence as it happens, and give a non-engineer a real approval surface. An
approval that can only be granted by typing a CLI flag is not a human-in-the-loop
control that a finance team would ever actually use.

Runs execute as background tasks in this process. That is right for a prototype
and wrong for production, where runs belong on a durable queue -- the kernel is
already written for it, since a run is one serializable state object plus an
event log. The README says so rather than pretending otherwise.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ..bootstrap import Operator, build_operator
from ..runtime.kernel import desktop_surface_available
from ..runtime.state import RunState

STATIC = Path(__file__).parent / "static"


class StartRequest(BaseModel):
    goal: str
    token: str = ""


class RespondRequest(BaseModel):
    response: str
    approved: bool | None = None


class RunManager:
    """Owns the operator and the in-flight run tasks."""

    def __init__(self) -> None:
        self._operator: Operator | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._queues: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def operator(self) -> Operator:
        if self._operator is None:
            self._operator = build_operator(require_llm=False)
            self._operator.events.subscribe(self._fan_out)
        return self._operator

    def _fan_out(self, event) -> None:
        """Push an event to every open SSE stream.

        Called from whichever context the kernel is running in, so hand off to
        the loop thread-safely rather than touching the queues directly.
        """
        payload = event.as_dict()
        loop = self._loop
        if loop is None:
            return
        for queue in list(self._queues):
            loop.call_soon_threadsafe(queue.put_nowait, payload)

    def subscribe(self) -> asyncio.Queue:
        # Capture the loop here as well as at start(): a browser may connect
        # before any run exists, and events from a resume would otherwise have
        # nowhere to go.
        self._loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._queues.discard(queue)

    def start(self, goal: str) -> str:
        from ..events.log import new_run_id

        run_id = new_run_id()
        self._loop = asyncio.get_running_loop()
        self._tasks[run_id] = asyncio.create_task(self._execute(goal, run_id))
        return run_id

    async def _execute(self, goal: str, run_id: str) -> None:
        try:
            await self.operator.kernel.start(goal, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI as an event
            self.operator.events.append(run_id, "run.failed", {"error": str(exc)})

    def respond(self, run_id: str, response: str, approved: bool | None) -> None:
        self._loop = asyncio.get_running_loop()
        self._tasks[f"{run_id}:resume"] = asyncio.create_task(
            self._resume(run_id, response, approved)
        )

    async def _resume(self, run_id: str, response: str, approved: bool | None) -> None:
        try:
            await self.operator.kernel.resume(run_id, response=response, approved=approved)
        except Exception as exc:  # noqa: BLE001
            self.operator.events.append(run_id, "run.failed", {"error": str(exc)})

    def state(self, run_id: str) -> RunState | None:
        snapshot = self.operator.events.load_snapshot(run_id)
        return RunState.model_validate(snapshot) if snapshot else None


def create_app() -> FastAPI:
    app = FastAPI(title="CentrAlign operator", docs_url=None, redoc_url=None)
    manager = RunManager()

    @app.get("/")
    async def index() -> FileResponse:
        # Never cache the shell. The dashboard is edited and reloaded constantly,
        # and a browser quietly serving yesterday's copy during a demo looks
        # exactly like a bug in the agent.
        return FileResponse(
            STATIC / "index.html",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        operator = manager.operator
        models = getattr(operator.llm, "members", None)
        model_label = ",".join(m.model for m in models) if models else operator.llm.model
        return {
            "provider": f"{operator.llm.name}/{model_label}",
            "tools": operator.registry.names,
            "tool_count": len(operator.registry),
            "surfaces": sorted({tool.surface for tool in operator.registry}),
            "policy": operator.policy.describe(),
            "documents": operator.memory.count(),
            "public_demo": operator.settings.public_demo,
            "needs_token": operator.settings.public_demo and bool(operator.settings.demo_token),
            "desktop_available": desktop_surface_available(),
        }

    @app.post("/api/sandbox/reset")
    async def reset_sandbox() -> dict[str, Any]:
        """Reseed the sandbox company.

        Proxied through the operator rather than called from the browser so the
        page does not need to know the sandbox's address, and so a cross-origin
        request is not required just to reset a demo.
        """
        import httpx as _httpx

        try:
            async with _httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(f"{manager.operator.settings.erp_url}/_sandbox/reset")
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - reported to the UI, not raised
            raise HTTPException(
                status_code=503,
                detail=f"could not reach the sandbox: {exc}. Is 'python -m sandbox.serve' running?",
            ) from exc
        return {"ok": True}

    @app.get("/api/runs")
    async def runs() -> dict[str, Any]:
        return {"runs": manager.operator.events.list_runs(50)}

    @app.post("/api/runs")
    async def start(request: StartRequest) -> dict[str, str]:
        if not request.goal.strip():
            raise HTTPException(status_code=422, detail="goal must not be empty")

        settings = manager.operator.settings
        if settings.public_demo:
            # Guard the only expensive action. Everything else on this instance
            # stays open, because the recorded runs are the interesting part.
            if not settings.demo_token:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "This is a read-only public instance: browse the recorded runs, "
                        "their decision streams and their evidence. To run the operator "
                        "yourself, clone the repo and run it locally."
                    ),
                )
            if not secrets.compare_digest(request.token.strip(), settings.demo_token):
                raise HTTPException(status_code=403, detail="Access code required to start a run.")

        return {"run_id": manager.start(request.goal.strip())}

    @app.get("/api/runs/{run_id}")
    async def run_state(run_id: str) -> dict[str, Any]:
        state = manager.state(run_id)
        if state is None:
            raise HTTPException(status_code=404, detail="no such run")
        pending = state.pending_human()
        return {
            "state": state.model_dump(mode="json"),
            "pending": pending.model_dump(mode="json") if pending else None,
        }

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, after: int = 0) -> dict[str, Any]:
        return {
            "events": [event.as_dict() for event in manager.operator.events.events(run_id, after_seq=after)]
        }

    @app.post("/api/runs/{run_id}/respond")
    async def respond(run_id: str, request: RespondRequest) -> dict[str, bool]:
        state = manager.state(run_id)
        if state is None:
            raise HTTPException(status_code=404, detail="no such run")
        if state.pending_human() is None:
            raise HTTPException(status_code=409, detail="this run is not waiting for anyone")
        manager.respond(run_id, request.response, request.approved)
        return {"ok": True}

    @app.get("/api/stream")
    async def stream() -> StreamingResponse:
        queue = manager.subscribe()

        async def generator():
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=20.0)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"  # keeps proxies from closing the stream
                        continue
                    yield f"data: {json.dumps(event, default=str)}\n\n"
            finally:
                manager.unsubscribe(queue)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/runs/{run_id}/artifact/{name}")
    async def artifact(run_id: str, name: str) -> FileResponse:
        directory = (manager.operator.settings.artifacts_dir / run_id).resolve()
        path = (directory / name).resolve()
        if directory not in path.parents or not path.exists():
            raise HTTPException(status_code=404, detail="no such artifact")
        return FileResponse(path)

    return app
