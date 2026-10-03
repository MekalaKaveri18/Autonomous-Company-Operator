"""Typed run state: the single serializable object a run is recovered from.

Three design points worth flagging, because they are the ones a reviewer should
push on:

**1. A plan is a living artifact, not a frozen DAG.** The planner emits a full
plan up front -- so a human can preview the whole intent, and so approvals can
be reasoned about before anything executes -- but the kernel may revise, insert
or drop remaining steps after any observation. A frozen DAG cannot react to
"the invoice references a PO that does not exist"; a pure step-at-a-time ReAct
loop has no plan to show anyone. This is the middle.

**2. Step arguments are resolved deterministically where possible.** Later
steps need data earlier steps discovered. Rather than spend a model call per
step turning the blackboard into arguments, the planner may write
``${steps.s2.data.total}`` and the kernel resolves it with plain code. The model
is consulted only when resolution *fails*. Cheap in the happy path, still
recoverable in the unhappy one.

**3. Risk tier is never self-reported by the model.** It is a property of the
tool and its arguments, computed by the policy engine. A model that could
declare its own actions low-risk could talk its way past every approval gate.
"""

from __future__ import annotations

import re
import time
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class RunStatus(str, Enum):
    CREATED = "created"
    UNDERSTANDING = "understanding"
    PLANNING = "planning"
    EXECUTING = "executing"
    AWAITING_HUMAN = "awaiting_human"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    ABORTED = "aborted"

    @property
    def terminal(self) -> bool:
        return self in {
            RunStatus.COMPLETED,
            RunStatus.PARTIAL,
            RunStatus.FAILED,
            RunStatus.ABORTED,
        }


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    AWAITING_HUMAN = "awaiting_human"


class CriterionStatus(str, Enum):
    UNKNOWN = "unknown"
    MET = "met"
    UNMET = "unmet"
    UNVERIFIABLE = "unverifiable"


class RiskTier(str, Enum):
    """Set by the policy engine from the tool and its arguments -- not by the model."""

    READ = "read"           # observes only
    WRITE = "write"         # mutates sandbox state, reversible
    SENSITIVE = "sensitive" # mutates records of record, or crosses a policy threshold
    EXTERNAL = "external"   # leaves the sandbox (email, payment) -- always gated


class Observation(BaseModel):
    """What actually happened, as opposed to what the agent intended.

    ``summary`` is model-facing prose. ``data`` is machine-facing and is what
    later steps and the verifier read, so a tool must never bury a fact only in
    the summary.
    """

    ok: bool
    summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    error: str | None = None
    error_kind: str | None = None
    retryable: bool = False
    duration_ms: int = 0

    def brief(self, limit: int = 1200) -> str:
        head = self.summary if self.ok else f"FAILED: {self.error or self.summary}"
        return head[:limit]


class AcceptanceCriterion(BaseModel):
    """One checkable condition that must hold for the goal to be done.

    ``check`` records *how* to confirm it by observation. Without that, a
    verifier degrades into asking the model whether it thinks it succeeded.
    """

    id: str
    text: str
    check: str = ""
    status: CriterionStatus = CriterionStatus.UNKNOWN
    evidence: list[str] = Field(default_factory=list)
    note: str = ""


class Step(BaseModel):
    id: str
    intent: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    expect: str = ""

    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    observation: Observation | None = None
    risk: RiskTier | None = None
    approval_id: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    origin: str = "plan"  # "plan" | "repair" | "replan" | "verification"

    @property
    def done(self) -> bool:
        """Did this step actually achieve its intent?

        Only SUCCEEDED counts. A skipped step is one we gave up on, so anything
        depending on it must not proceed either -- a dependent that ran anyway
        would be acting on data its prerequisite never established, which in an
        approvals workflow means approving something nobody checked.
        """
        return self.status is StepStatus.SUCCEEDED


class Plan(BaseModel):
    #: 0 means "no plan yet"; the first real plan is version 1.
    version: int = 0
    rationale: str = ""
    steps: list[Step] = Field(default_factory=list)

    def by_id(self, step_id: str) -> Step | None:
        return next((s for s in self.steps if s.id == step_id), None)

    def next_runnable(self) -> Step | None:
        """First pending step whose dependencies have all completed.

        A step whose dependency *failed* is not runnable; the adapt phase
        decides whether to repair, drop it, or replan around it.
        """
        for step in self.steps:
            if step.status is not StepStatus.PENDING:
                continue
            if all((self.by_id(dep) or Step(id=dep, intent="", tool="")).done for dep in step.depends_on):
                return step
        return None

    def blocked_steps(self) -> list[Step]:
        """Pending steps whose prerequisites can no longer be satisfied.

        Marking these skipped in turn cascades down the chain on the next pass,
        so an abandoned step takes its whole dependent subtree with it.
        """
        unachievable = {StepStatus.FAILED, StepStatus.SKIPPED}
        return [
            step
            for step in self.steps
            if step.status is StepStatus.PENDING
            and any(
                (self.by_id(dep) or Step(id=dep, intent="", tool="")).status in unachievable
                for dep in step.depends_on
            )
        ]


class HumanRequest(BaseModel):
    """An approval or a question. Both pause the run; both are resumable."""

    id: str
    kind: Literal["approval", "question"]
    #: Why the run paused, which decides what resuming *does*:
    #:
    #: ``policy``  the gate stopped a step before it ran, so an approval re-arms
    #:             that step and it executes.
    #: ``tool``    a tool itself asked for a person (ask_human, escalate_to_human).
    #:             The answer IS the step's result -- re-running the step would
    #:             just raise the same question again, forever.
    #: ``intake``  a blocking question asked before any plan existed.
    origin: Literal["policy", "tool", "intake"] = "policy"
    step_id: str | None = None
    prompt: str
    context: dict[str, Any] = Field(default_factory=dict)
    options: list[str] = Field(default_factory=list)
    risk: RiskTier | None = None
    created_at: float = Field(default_factory=time.time)

    resolved: bool = False
    response: str | None = None
    approved: bool | None = None
    resolved_at: float | None = None


class Verdict(BaseModel):
    """The verifier's finding, produced from re-observed state."""

    complete: bool = False
    confidence: float = 0.0
    summary: str = ""
    criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    unmet: list[str] = Field(default_factory=list)
    remediation: list[str] = Field(default_factory=list)

    @property
    def met_count(self) -> int:
        return sum(1 for c in self.criteria if c.status is CriterionStatus.MET)


class Budget(BaseModel):
    """Consumption counters, checked before every step and model call."""

    max_steps: int = 40
    max_llm_calls: int = 80
    max_consecutive_failures: int = 3
    max_replans: int = 4
    #: Continuation cycles: how many times the planner may be asked to carry on
    #: after its previous plan ran out. Distinct from max_replans, which bounds
    #: *failure-driven* replanning -- progressive planning is normal operation,
    #: not recovery, and conflating them would stop long tasks halfway.
    max_plan_cycles: int = 8
    wall_clock_seconds: int = 900

    steps_used: int = 0
    llm_calls_used: int = 0
    consecutive_failures: int = 0
    replans_used: int = 0
    plan_cycles_used: int = 0
    started_at: float = Field(default_factory=time.time)

    def exceeded(self) -> str | None:
        """Return the name of the first exhausted limit, or ``None``."""
        if self.steps_used >= self.max_steps:
            return f"step limit ({self.max_steps})"
        if self.llm_calls_used >= self.max_llm_calls:
            return f"model-call limit ({self.max_llm_calls})"
        if self.consecutive_failures >= self.max_consecutive_failures:
            return f"consecutive-failure limit ({self.max_consecutive_failures})"
        if self.replans_used > self.max_replans:
            return f"replan limit ({self.max_replans})"
        if self.plan_cycles_used > self.max_plan_cycles:
            return f"planning-cycle limit ({self.max_plan_cycles})"
        if time.time() - self.started_at >= self.wall_clock_seconds:
            return f"wall-clock limit ({self.wall_clock_seconds}s)"
        return None

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at


class RunState(BaseModel):
    """Everything about a run, serializable so recovery is a plain load."""

    run_id: str
    goal: str
    status: RunStatus = RunStatus.CREATED

    objective: str = ""
    deliverables: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)

    plan: Plan = Field(default_factory=Plan)
    #: Set by the planner when it reports nothing remains. Ends the execution
    #: loop; verification still runs and can disagree.
    objective_complete: bool = False
    #: Blackboard. Durable, named working memory that later steps and the
    #: verifier read. Distinct from step observations, which are raw history.
    facts: dict[str, Any] = Field(default_factory=dict)

    human_requests: list[HumanRequest] = Field(default_factory=list)
    verdict: Verdict | None = None
    budget: Budget = Field(default_factory=Budget)

    #: Short, append-only narrative of what the agent decided and why. Fed back
    #: into prompts as compressed history so token use stays flat as a run
    #: lengthens, rather than growing with the full observation log.
    journal: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: float = Field(default_factory=time.time)
    finished_at: float | None = None

    # -- derived views ------------------------------------------------------
    def pending_human(self) -> HumanRequest | None:
        return next((r for r in self.human_requests if not r.resolved), None)

    def request_by_id(self, request_id: str) -> HumanRequest | None:
        return next((r for r in self.human_requests if r.id == request_id), None)

    def note(self, text: str) -> None:
        self.journal.append(text)

    def completed_steps(self) -> list[Step]:
        return [s for s in self.plan.steps if s.status is StepStatus.SUCCEEDED]

    def failed_steps(self) -> list[Step]:
        return [s for s in self.plan.steps if s.status is StepStatus.FAILED]

    def next_step_id(self, prefix: str = "s") -> str:
        return f"{prefix}{len(self.plan.steps) + 1}"


# ---------------------------------------------------------------------------
# Deterministic ${...} reference resolution against the blackboard
# ---------------------------------------------------------------------------

_REF = re.compile(r"\$\{([^}]+)\}")


class UnresolvedReference(KeyError):
    """A ``${...}`` reference did not resolve -- hand the step to LLM repair."""


def lookup_path(root: Any, path: str) -> Any:
    """Resolve a dotted path with optional list indexing: ``a.b[0].c``.

    Intentionally tiny. A real JSONPath engine invites the planner to emit
    expressions we cannot audit; this covers what plans actually need.
    """
    current = root
    for raw in path.split("."):
        if not raw:
            raise UnresolvedReference(path)
        # re.split with a capture group yields ['name', '0', '', ...]; the
        # empty strings are the zero-width gaps between adjacent brackets.
        parts = [p for p in re.split(r"\[(\d+)\]", raw) if p != ""]
        name, indexes = parts[0], parts[1:]

        if isinstance(current, dict):
            if name not in current:
                raise UnresolvedReference(f"{path} (missing key {name!r})")
            current = current[name]
        else:
            if not hasattr(current, name):
                raise UnresolvedReference(f"{path} (no attribute {name!r})")
            current = getattr(current, name)

        for index in indexes:
            try:
                current = current[int(index)]
            except (IndexError, KeyError, TypeError) as exc:
                raise UnresolvedReference(f"{path} (bad index [{index}])") from exc
    return current


def resolve_refs(value: Any, context: dict[str, Any]) -> Any:
    """Substitute every ``${path}`` in a nested structure.

    A string that is *exactly* one reference becomes the referenced value with
    its type intact (so a number stays a number). A reference embedded in a
    larger string is interpolated as text.
    """
    if isinstance(value, dict):
        return {k: resolve_refs(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_refs(v, context) for v in value]
    if not isinstance(value, str):
        return value

    whole = _REF.fullmatch(value.strip())
    if whole:
        return lookup_path(context, whole.group(1).strip())

    def substitute(match: re.Match[str]) -> str:
        resolved = lookup_path(context, match.group(1).strip())
        return resolved if isinstance(resolved, str) else str(resolved)

    return _REF.sub(substitute, value)


def build_ref_context(state: RunState) -> dict[str, Any]:
    """The namespace that ``${...}`` resolves against."""
    return {
        "facts": state.facts,
        "steps": {
            step.id: {
                "data": (step.observation.data if step.observation else {}),
                "summary": (step.observation.summary if step.observation else ""),
                "ok": (step.observation.ok if step.observation else False),
            }
            for step in state.plan.steps
        },
        "goal": state.goal,
        "objective": state.objective,
    }
