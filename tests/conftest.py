"""Fixtures for driving the real kernel with a scripted model.

The point of these tests is to pin down *kernel* behaviour -- retry, adaptation,
approval gating, verification independence -- which is the part that must be
correct regardless of which model is plugged in. Scripting the model makes those
behaviours deterministic and assertable; testing them against a live model would
measure the model, slowly and flakily.

Model responses are built by the helpers below so a test reads as the decision
sequence it is exercising, not as a wall of JSON.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import pytest

from centralign.config import Budget, Settings
from centralign.events.log import EventLog
from centralign.llm.replay import ScriptedProvider
from centralign.memory.store import MemoryStore
from centralign.policy.guard import PolicyGuard
from centralign.runtime.kernel import Kernel
from centralign.runtime.state import Observation, RiskTier
from centralign.tools.base import FailureKind, Tool, ToolContext, ToolFailure
from centralign.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# scripted model responses
# ---------------------------------------------------------------------------


def intake(
    objective: str = "Do the thing",
    criteria: Sequence[tuple[str, str]] = (("ac1", "The thing is done"),),
    *,
    blocking_question: str = "",
) -> str:
    return json.dumps(
        {
            "objective": objective,
            "deliverables": ["the thing"],
            "acceptance_criteria": [
                {"id": cid, "text": text, "check": f"observe that {text.lower()}"}
                for cid, text in criteria
            ],
            "assumptions": [],
            "out_of_scope": [],
            "blocking_question": blocking_question,
        }
    )


def plan(
    *steps: dict[str, Any],
    rationale: str = "because the SOP says so",
    complete: bool = True,
) -> str:
    """A scripted plan.

    ``complete`` is the planner's ``objective_complete`` signal. It defaults to
    True because most tests script a plan that finishes the work in one pass;
    a test exercising progressive planning passes ``complete=False`` and must
    then script the follow-on plan too.
    """
    return json.dumps(
        {
            "rationale": rationale,
            "objective_complete": complete,
            "steps": [
                {
                    "id": step.get("id", f"s{index}"),
                    "intent": step.get("intent", "do a thing"),
                    "tool": step["tool"],
                    "args_json": json.dumps(step.get("args", {})),
                    "depends_on": step.get("depends_on", []),
                    "expect": step.get("expect", ""),
                }
                for index, step in enumerate(steps, start=1)
            ],
        }
    )


def step(tool: str, *, id: str | None = None, args: dict | None = None, **extra) -> dict:
    out = {"tool": tool, "args": args or {}}
    if id:
        out["id"] = id
    out.update(extra)
    return out


def adapt(action: str, **extra) -> str:
    payload = {"analysis": "something went wrong", "action": action, "reason": "test reason"}
    payload.update(extra)
    return json.dumps(payload)


def repair(args: dict) -> str:
    return json.dumps({"args_json": json.dumps(args), "reason": "fixed the arguments"})


def probes(*items: dict[str, Any]) -> str:
    return json.dumps(
        {
            "reasoning": "re-read the systems of record",
            "probes": [
                {
                    "id": item.get("id", f"p{index}"),
                    "criterion_id": item.get("criterion_id", "ac1"),
                    "tool": item["tool"],
                    "args_json": json.dumps(item.get("args", {})),
                    "looking_for": item.get("looking_for", "the expected state"),
                }
                for index, item in enumerate(items, start=1)
            ],
        }
    )


def verdict(
    *statuses: tuple[str, str],
    complete: bool = True,
    confidence: float = 0.9,
    remediation: Sequence[str] = (),
) -> str:
    return json.dumps(
        {
            "summary": "verification summary",
            "complete": complete,
            "confidence": confidence,
            "criteria": [
                {"id": cid, "status": status, "note": f"observed: {status}"}
                for cid, status in statuses
            ],
            "remediation": list(remediation),
        }
    )


def report(headline: str = "Done.") -> str:
    return json.dumps(
        {
            "headline": headline,
            "narrative": "This is what happened.",
            "needs_attention": [],
            "learnings": [],
        }
    )


# ---------------------------------------------------------------------------
# test tools
# ---------------------------------------------------------------------------


class RecordingTool(Tool):
    """Base for test tools: records every invocation for assertions."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []


class AlwaysOk(RecordingTool):
    name = "fake_read"
    surface = "fake"
    description = "A read that always succeeds."
    args_schema = {"type": "object", "properties": {"note": {"type": "string"}}, "required": []}
    risk = RiskTier.READ

    async def execute(self, args, ctx) -> Observation:
        self.calls.append(args)
        return Observation(ok=True, summary="read ok", data={"value": 42, "note": args.get("note", "")})


class Flaky(RecordingTool):
    name = "fake_flaky"
    surface = "fake"
    description = "Fails transiently a set number of times, then succeeds."
    args_schema = {"type": "object", "properties": {}, "required": []}
    risk = RiskTier.READ

    def __init__(self, failures: int = 1) -> None:
        super().__init__()
        self.remaining = failures

    async def execute(self, args, ctx) -> Observation:
        self.calls.append(args)
        if self.remaining > 0:
            self.remaining -= 1
            raise ToolFailure("service briefly unavailable", kind=FailureKind.UNAVAILABLE)
        return Observation(ok=True, summary="flaky ok after retry", data={"ok": True})


class NotFound(RecordingTool):
    name = "fake_missing"
    surface = "fake"
    description = "Always reports the target does not exist."
    args_schema = {"type": "object", "properties": {}, "required": []}
    risk = RiskTier.READ

    async def execute(self, args, ctx) -> Observation:
        self.calls.append(args)
        raise ToolFailure("PO-9999 does not exist", kind=FailureKind.NOT_FOUND)


class StrictArgs(RecordingTool):
    name = "fake_strict"
    surface = "fake"
    description = "Requires a 'target' argument."
    args_schema = {
        "type": "object",
        "properties": {"target": {"type": "string"}},
        "required": ["target"],
    }
    risk = RiskTier.READ

    async def execute(self, args, ctx) -> Observation:
        self.calls.append(args)
        return Observation(ok=True, summary=f"handled {args['target']}", data={"target": args["target"]})


class SensitiveWrite(RecordingTool):
    name = "fake_approve"
    surface = "fake"
    description = "A sensitive, irreversible write."
    args_schema = {"type": "object", "properties": {"amount": {"type": "number"}}, "required": []}
    risk = RiskTier.SENSITIVE
    reversible = False

    async def execute(self, args, ctx) -> Observation:
        self.calls.append(args)
        return Observation(ok=True, summary="approved", data={"approved": True})

    def preview(self, args) -> str:
        return f"Approve a payment of {args.get('amount')}"


class ProgressingTool(RecordingTool):
    """Same arguments every time, but a different result each time.

    Models the real case the loop guard must not kill: clicking "Grant access"
    repeatedly with different form contents, where the action looks identical but
    the world moves each time.
    """

    name = "fake_progress"
    surface = "fake"
    description = "Succeeds with a different observation on every call."
    args_schema = {"type": "object", "properties": {}, "required": []}
    risk = RiskTier.READ

    def __init__(self) -> None:
        super().__init__()
        self.n = 0

    async def execute(self, args, ctx) -> Observation:
        self.calls.append(args)
        self.n += 1
        return Observation(ok=True, summary=f"granted item {self.n}", data={"n": self.n})


class PlainWrite(RecordingTool):
    name = "fake_write"
    surface = "fake"
    description = "An ordinary reversible write."
    args_schema = {"type": "object", "properties": {}, "required": []}
    risk = RiskTier.WRITE

    async def execute(self, args, ctx) -> Observation:
        self.calls.append(args)
        return Observation(ok=True, summary="written", data={"written": True})


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        provider="scripted",
        model="scripted",
        llm_cache=False,
        headless=True,
        company_dir=tmp_path / "company",
        drive_dir=tmp_path / "drive",
        var_dir=tmp_path / "var",
        artifacts_dir=tmp_path / "artifacts",
        budget=Budget(max_steps=25, max_llm_calls=40, wall_clock_seconds=60),
    )


@pytest.fixture
def tools() -> dict[str, Tool]:
    return {
        tool.name: tool
        for tool in (
            AlwaysOk(),
            Flaky(),
            NotFound(),
            StrictArgs(),
            SensitiveWrite(),
            PlainWrite(),
            ProgressingTool(),
        )
    }


@pytest.fixture
def make_kernel(settings: Settings, tools: dict[str, Tool]):
    """Build a kernel wired to a scripted model. Returns (kernel, provider)."""

    created: list[tuple[Kernel, ScriptedProvider]] = []

    def build(
        responses: Sequence[str], *, policy: PolicyGuard | None = None, extra_tools: Sequence[Tool] = ()
    ) -> tuple[Kernel, ScriptedProvider]:
        settings.ensure_dirs()
        provider = ScriptedProvider(list(responses))
        registry = ToolRegistry([*tools.values(), *extra_tools])
        kernel = Kernel(
            settings=settings,
            llm=provider,
            registry=registry,
            memory=MemoryStore(settings.memory_db),
            policy=policy or PolicyGuard({"autonomy": "standard"}),
            events=EventLog(settings.event_db),
        )
        created.append((kernel, provider))
        return kernel, provider

    yield build

    for kernel, _ in created:
        kernel.memory.close()
        kernel.events.close()


@pytest.fixture
def event_types():
    """Collect emitted event types from a kernel's log."""

    def collect(kernel: Kernel, run_id: str) -> list[str]:
        return [event.type for event in kernel.events.events(run_id)]

    return collect
