"""The operator runtime.

Goal -> Understand -> Plan -> Execute -> Observe -> Adapt -> Verify -> Complete,
with the loop closed: verification can send the run back to planning, bounded.

The kernel owns control flow and nothing else. It does not know what an invoice
is, it never names a tool, and it contains no domain logic -- all of that lives in
the tool registry, the connector YAML and company memory. That is what makes the
same kernel run a different task.

Three properties were designed for before features:

**It always terminates.** Every loop is bounded by :class:`Budget`, and a
no-progress detector catches the case budgets do not: an agent that keeps doing
something successfully without advancing. An agent that cannot stop is worse than
one that cannot start.

**It fails loudly and partially.** A step that cannot be recovered marks its work
item, and the run continues on the others. The outcome is a truthful partial
result, never a silent success.

**It can be interrupted and resumed.** Approvals and questions suspend the run to
a snapshot. Resuming days later picks up where it stopped, because the state
object is the whole run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any, Callable

from ..config import Settings
from ..evidence.bundle import write_evidence_bundle
from ..llm.base import LLMError, LLMProvider, Message
from ..memory.store import EPISODIC, MemoryStore
from ..events.log import EventLog, new_run_id
from ..policy.guard import PolicyDecision, PolicyGuard
from ..tools.base import HumanInputRequired, Tool, ToolContext
from ..tools.registry import ToolRegistry
from . import prompts
from .state import (
    AcceptanceCriterion,
    CriterionStatus,
    HumanRequest,
    Observation,
    Plan,
    RiskTier,
    RunState,
    RunStatus,
    Step,
    StepStatus,
    UnresolvedReference,
    build_ref_context,
    resolve_refs,
)
from .verifier import Verifier

#: Transient failures retried in place, before the model is consulted at all.
MAX_IN_PLACE_RETRIES = 3
#: Verification rounds, i.e. one remediation attempt after a failed verdict.
MAX_VERIFY_ROUNDS = 2
#: Identical successful actions before we call it thrashing.
MAX_REPEATED_ACTIONS = 3

#: Failure kinds that report a fact about the world rather than a fault in it.
#: From a read-only tool these are answers, and answers should not count toward
#: the consecutive-failure budget.
_INFORMATIONAL = {"not_found", "conflict"}


class _Paused(Exception):
    """Internal signal: the run is suspended awaiting a person."""


class Kernel:
    def __init__(
        self,
        *,
        settings: Settings,
        llm: LLMProvider,
        registry: ToolRegistry,
        memory: MemoryStore,
        policy: PolicyGuard,
        events: EventLog,
        company_name: str = "CentrAlign",
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.registry = registry
        self.memory = memory
        self.policy = policy
        self.events = events
        self.company_name = company_name
        self._ctx: ToolContext | None = None
        self._action_counts: dict[str, int] = {}
        #: Action signatures already shown to make no progress, mapped to the
        #: result they kept producing. Detection is necessarily after the fact --
        #: we only know an action changed nothing once we have seen its result --
        #: so remembering them short-circuits the *next* identical attempt.
        self._looping: dict[str, Observation] = {}
        self._verification_evidence: str = ""

    # -- public API ---------------------------------------------------------
    async def start(self, goal: str, *, run_id: str | None = None) -> RunState:
        state = RunState(run_id=run_id or new_run_id(), goal=goal.strip())
        state.budget = _budget_from(self.settings)
        self._emit(state, "run.created", {"goal": state.goal, "policy": self.policy.describe()})
        self._save(state)
        return await self._drive(state)

    async def resume(
        self, run_id: str, *, response: str, approved: bool | None = None
    ) -> RunState:
        snapshot = self.events.load_snapshot(run_id)
        if snapshot is None:
            raise KeyError(f"no run with id {run_id!r}")
        state = RunState.model_validate(snapshot)

        request = state.pending_human()
        if request is None:
            self._emit(state, "resume.noop", {"detail": "no pending human request"})
            return state

        request.resolved = True
        request.response = response
        request.resolved_at = time.time()
        request.approved = approved if approved is not None else _reads_as_approval(response)

        self._emit(
            state,
            "human.responded",
            {
                "request_id": request.id,
                "kind": request.kind,
                "approved": request.approved,
                "response": response,
            },
        )

        step = state.plan.by_id(request.step_id) if request.step_id else None
        if step is not None:
            if request.origin == "policy" and request.kind == "approval" and request.approved:
                # The gate stopped this step before it ran. Re-arm it; the
                # recorded approval lets it past the gate exactly once.
                step.status = StepStatus.PENDING
                step.approval_id = request.id
                state.note(f"{step.id}: approved by a person -- {response[:160]}")
            elif request.origin == "policy" and request.kind == "approval":
                step.status = StepStatus.FAILED
                step.observation = Observation(
                    ok=False,
                    summary=f"A person declined this action: {response}",
                    error=f"declined by a person: {response}",
                    error_kind="permission_denied",
                    retryable=False,
                )
                state.note(f"{step.id}: declined by a person -- {response[:160]}")
            else:
                # The step *was* the act of asking -- ask_human, escalate_to_human,
                # or an intake question. The answer is its result. Re-running it
                # would raise the same question again and the run would never
                # move past it.
                step.status = StepStatus.SUCCEEDED
                step.observation = Observation(
                    ok=True,
                    summary=(
                        f"A person {'approved' if request.approved else 'responded'}: {response}"
                    ),
                    data={"response": response, "approved": request.approved},
                )
                state.note(f"{step.id}: answered by a person -- {response[:160]}")

        # A fresh wall clock: time spent waiting for a human is not the agent's.
        state.budget.started_at = time.time()
        state.status = RunStatus.EXECUTING
        self._save(state)
        return await self._drive(state)

    # -- orchestration ------------------------------------------------------
    async def _drive(self, state: RunState) -> RunState:
        try:
            if state.status in {RunStatus.CREATED, RunStatus.UNDERSTANDING}:
                await self._understand(state)
            if not state.plan.steps:
                await self._plan(state)

            rounds = 0
            while True:
                try:
                    await self._pursue_objective(state)
                except _Abort as abort:
                    # Stop executing, but still verify and report. An abort that
                    # produced no evidence bundle would leave whatever *was* done
                    # undocumented, which is the worst of both outcomes.
                    state.note(f"Execution aborted: {abort}")
                    await self._verify(state)
                    break

                rounds += 1
                verdict = await self._verify(state)
                if verdict.complete or rounds >= MAX_VERIFY_ROUNDS:
                    break
                if not verdict.remediation:
                    break
                if state.budget.exceeded():
                    break

                state.note(
                    "Verification found the outcome incomplete; attempting remediation."
                )
                self._emit(
                    state,
                    "verification.remediation_started",
                    {"unmet": verdict.unmet, "remediation": verdict.remediation},
                )
                await self._plan(
                    state,
                    revision_reason=(
                        "Verification of the completed work found these criteria unmet: "
                        + "; ".join(verdict.unmet)
                        + ". What still needs to happen: "
                        + "; ".join(verdict.remediation)
                        + ". Plan only the remaining work."
                    ),
                )

            await self._complete(state)

        except _Paused:
            state.status = RunStatus.AWAITING_HUMAN
            self._save(state)
        except LLMError as exc:
            state.status = RunStatus.FAILED
            state.error = f"model provider failure: {exc}"
            state.finished_at = time.time()
            self._emit(state, "run.failed", {"error": state.error})
            self._save(state)
        finally:
            if not state.status.terminal and state.status is not RunStatus.AWAITING_HUMAN:
                self._save(state)
            if state.status is not RunStatus.AWAITING_HUMAN:
                await self._teardown()

        return state

    # -- phase: understand --------------------------------------------------
    async def _understand(self, state: RunState) -> None:
        state.status = RunStatus.UNDERSTANDING
        self._emit(state, "intake.started", {})

        excerpts = self._recall(state.goal, limit=5)
        raw = await self._ask(
            state,
            prompts.intake_prompt(
                state.goal, memory_excerpts=excerpts, catalog=self.registry.render_catalog()
            ),
            prompts.INTAKE_SCHEMA,
            max_tokens=3000,
        )

        state.objective = str(raw.get("objective", "")).strip() or state.goal
        state.deliverables = [str(d) for d in (raw.get("deliverables") or [])]
        state.assumptions = [str(a) for a in (raw.get("assumptions") or [])]
        state.out_of_scope = [str(o) for o in (raw.get("out_of_scope") or [])]
        state.acceptance_criteria = [
            AcceptanceCriterion(
                id=str(item.get("id") or f"ac{index}"),
                text=str(item.get("text", "")),
                check=str(item.get("check", "")),
            )
            for index, item in enumerate(raw.get("acceptance_criteria") or [], start=1)
            if item.get("text")
        ]

        self._emit(
            state,
            "intake.completed",
            {
                "objective": state.objective,
                "deliverables": state.deliverables,
                "acceptance_criteria": [c.model_dump(mode="json") for c in state.acceptance_criteria],
                "assumptions": state.assumptions,
            },
        )

        question = str(raw.get("blocking_question", "")).strip()
        if question:
            self._request_human(
                state,
                kind="question",
                prompt=question,
                context={"phase": "intake"},
                step_id=None,
                origin="intake",
            )

    # -- phase: plan --------------------------------------------------------
    async def _plan(
        self, state: RunState, *, revision_reason: str = "", continuation: bool = False
    ) -> None:
        state.status = RunStatus.PLANNING
        # Only failure-driven replanning spends the replan budget. Continuation
        # is normal progress and has its own cycle budget.
        if revision_reason and not continuation:
            state.budget.replans_used += 1

        excerpts = self._recall(f"{state.goal} {state.objective}", limit=5)
        raw = await self._ask(
            state,
            prompts.plan_prompt(
                state,
                catalog=self.registry.render_catalog(),
                memory_excerpts=excerpts,
                revision_reason=revision_reason,
            ),
            prompts.PLAN_SCHEMA,
            max_tokens=6000,
        )

        kept = [s for s in state.plan.steps if s.status is not StepStatus.PENDING]
        new_steps: list[Step] = []
        existing_ids = {s.id for s in kept}
        for index, item in enumerate(raw.get("steps") or [], start=1):
            step = _step_from(item, fallback_id=f"r{state.plan.version + 1}-{index}")
            if step is None:
                continue
            # A revision must not reuse an id already spent, or history is lost.
            while step.id in existing_ids:
                step.id = f"{step.id}b"
            existing_ids.add(step.id)
            step.origin = "replan" if revision_reason else "plan"
            new_steps.append(step)

        state.objective_complete = bool(raw.get("objective_complete", False))

        if not new_steps and not kept and not state.objective_complete:
            raise LLMError("the planner returned no usable steps")

        state.plan = Plan(
            version=state.plan.version + 1,
            rationale=str(raw.get("rationale", "")),
            steps=kept + new_steps,
        )
        self._emit(
            state,
            "plan.revised" if revision_reason else "plan.created",
            {
                "version": state.plan.version,
                "rationale": state.plan.rationale,
                "objective_complete": state.objective_complete,
                "continuation": continuation,
                "reason": revision_reason,
                "steps": [
                    {"id": s.id, "tool": s.tool, "intent": s.intent, "args": s.args}
                    for s in new_steps
                ],
            },
        )
        self._save(state)

    # -- progressive planning ----------------------------------------------
    async def _pursue_objective(self, state: RunState) -> None:
        """Execute, and when the plan runs dry, ask the planner to carry on.

        A plan running out is not the same as the work being finished. A task
        spanning many items is planned progressively: discover, act on what you
        now know, then plan the next part with everything you have learned. If
        plan exhaustion were treated as completion, the operator would do its
        discovery pass and stop -- which is exactly what it did before this loop
        existed.

        Termination is bounded three ways: the planner must positively declare
        ``objective_complete``, the plan-cycle budget caps continuations, and a
        cycle that produces no new steps ends the loop. Verification still runs
        afterwards and can disagree with all of it.
        """
        while True:
            await self._execute_until_blocked(state)

            if state.objective_complete:
                state.note("Planner reports the objective is complete.")
                return

            exhausted = state.budget.exceeded()
            if exhausted:
                return

            if state.plan.next_runnable() is not None:
                continue  # new work appeared (an inserted repair step)

            state.budget.plan_cycles_used += 1
            if state.budget.plan_cycles_used > state.budget.max_plan_cycles:
                state.note(
                    f"Stopped after {state.budget.max_plan_cycles} planning cycles "
                    "without the objective being reported complete."
                )
                self._emit(state, "budget.exceeded", {"limit": "planning cycles"})
                return

            before = len(state.plan.steps)
            self._emit(
                state,
                "plan.continuing",
                {"cycle": state.budget.plan_cycles_used, "steps_so_far": before},
            )
            await self._plan(
                state,
                revision_reason=(
                    "Your previous plan has been fully executed, but the objective is "
                    "not finished. Plan the next part of the work, using everything "
                    "established so far. Do not repeat completed steps. If every part "
                    "of the objective is genuinely done, set objective_complete true "
                    "and return no steps."
                ),
                continuation=True,
            )
            if state.plan.next_runnable() is None:
                state.note("Planner produced no further runnable steps.")
                return

    # -- phase: execute / observe / adapt -----------------------------------
    async def _execute_until_blocked(self, state: RunState) -> None:
        state.status = RunStatus.EXECUTING
        while True:
            exhausted = state.budget.exceeded()
            if exhausted:
                state.note(f"Stopped: {exhausted} reached.")
                self._emit(state, "budget.exceeded", {"limit": exhausted})
                return

            step = state.plan.next_runnable()
            if step is None:
                blocked = state.plan.blocked_steps()
                if not blocked:
                    return
                # Dependencies failed. Drop the orphans and let verification
                # judge what that cost; replanning here risks an infinite cycle.
                for orphan in blocked:
                    orphan.status = StepStatus.SKIPPED
                    orphan.observation = Observation(
                        ok=False,
                        summary="Skipped: a step it depended on did not succeed.",
                        error_kind="precondition_failed",
                    )
                    self._emit(
                        state, "step.skipped", {"reason": "dependency failed"}, step_id=orphan.id
                    )
                state.note(f"Skipped {len(blocked)} step(s) whose dependencies failed.")
                continue

            await self._run_step(state, step)
            self._save(state)

    async def _run_step(self, state: RunState, step: Step) -> None:
        state.budget.steps_used += 1
        step.attempts += 1
        step.status = StepStatus.RUNNING
        step.started_at = time.time()

        # 1. resolve references from the blackboard
        try:
            args = resolve_refs(step.args, build_ref_context(state))
        except UnresolvedReference as exc:
            repaired = await self._repair_args(state, step, problem=f"unresolved reference: {exc}")
            if repaired is None:
                self._fail_step(
                    state, step, f"could not resolve step arguments: {exc}", kind="invalid_args"
                )
                return
            args = repaired

        if not self.registry.has(step.tool):
            self._fail_step(
                state,
                step,
                f"tool {step.tool!r} does not exist; available: {', '.join(self.registry.names)}",
                kind="not_found",
            )
            await self._adapt(state, step)
            return

        tool = self.registry.get(step.tool)

        # 2. policy gate -- computed from the tool and the real arguments
        decision = self.policy.assess(tool, args)
        step.risk = decision.risk
        if not decision.allowed:
            self._fail_step(state, step, f"blocked by company policy: {decision.reason}", kind="permission_denied")
            self._emit(
                state,
                "policy.denied",
                {"tool": tool.name, "reason": decision.reason, "rules": decision.matched_rules},
                step_id=step.id,
            )
            await self._adapt(state, step)
            return

        if decision.requires_approval and not step.approval_id:
            self._emit(
                state,
                "policy.approval_required",
                {
                    "tool": tool.name,
                    "risk": decision.risk.value,
                    "reason": decision.reason,
                    "rules": decision.matched_rules,
                },
                step_id=step.id,
            )
            step.status = StepStatus.AWAITING_HUMAN
            self._request_human(
                state,
                kind="approval",
                prompt=(
                    f"Approve this action?\n\n{tool.preview(args)}\n\n"
                    f"Why you are being asked: {decision.reason}"
                ),
                context={
                    "tool": tool.name,
                    "args": args,
                    "risk": decision.risk.value,
                    "irreversible": not tool.reversible,
                    "policy_rules": decision.matched_rules,
                    "citations": decision.citations,
                },
                step_id=step.id,
                risk=decision.risk,
                origin="policy",
            )

        # 3. execute
        signature = f"{tool.name}:{json.dumps(args, sort_keys=True, default=str)[:300]}"
        remembered = self._looping.get(signature)
        if remembered is not None:
            if remembered.ok:
                # It succeeded before and changes nothing when repeated -- a tab
                # already open, a file already filed. The step's intent is
                # satisfied, so satisfy it from the previous result rather than
                # doing it again.
                step.status = StepStatus.SUCCEEDED
                step.observation = remembered
                step.ended_at = time.time()
                self._emit(
                    state,
                    "step.already_satisfied",
                    {"tool": tool.name, "detail": "repeating this would change nothing"},
                    step_id=step.id,
                )
                return
            self._fail_step(
                state,
                step,
                f"{tool.name} with these arguments was already shown to make no "
                "progress; not attempting it again",
                kind="internal",
            )
            return

        self._emit(
            state,
            "step.started",
            {"tool": tool.name, "intent": step.intent, "args": args, "risk": decision.risk.value, "attempt": step.attempts},
            step_id=step.id,
        )

        try:
            observation = await tool.invoke(args, self._context(state))
        except HumanInputRequired as pause:
            step.status = StepStatus.AWAITING_HUMAN
            # _request_human always raises _Paused, suspending the run here.
            self._request_human(
                state,
                kind=pause.kind,
                prompt=pause.prompt,
                context={"tool": tool.name, **pause.context},
                step_id=step.id,
                options=pause.options,
                risk=pause.risk,
                origin="tool",
            )
            raise AssertionError("unreachable: _request_human must suspend the run")

        step.observation = observation
        step.ended_at = time.time()
        step.args = args  # record what was actually sent, not what was planned

        self._emit(
            state,
            "step.observed",
            {
                "ok": observation.ok,
                "summary": observation.brief(1500),
                "error_kind": observation.error_kind,
                "artifacts": observation.artifacts,
                "duration_ms": observation.duration_ms,
            },
            step_id=step.id,
        )

        # 4. loop guard. Keyed on the action *and its outcome*, checked after the
        # fact. Repetition alone is not a loop: clicking "Grant access" four times
        # with different form contents is exactly right, and an earlier version of
        # this guard keyed only on (tool, args) and killed that legitimate work.
        # Being stuck means the same action producing the same result over and
        # over, which is what this measures.
        outcome = hashlib.sha1(
            f"{signature}|{observation.ok}|{observation.brief(600)}".encode("utf-8")
        ).hexdigest()
        self._action_counts[outcome] = self._action_counts.get(outcome, 0) + 1
        if self._action_counts[outcome] > MAX_REPEATED_ACTIONS:
            self._looping[signature] = observation
            self._emit(
                state,
                "loop.detected",
                {
                    "tool": tool.name,
                    "repeats": self._action_counts[outcome],
                    "benign": observation.ok,
                    "signature": signature[:200],
                },
                step_id=step.id,
            )
            if observation.ok:
                # Benign repetition: the action keeps working and keeps leaving
                # the world as the plan wants it. Failing the step here was a
                # false positive that cascaded -- dependents were abandoned, the
                # consecutive-failure budget tripped, and a healthy run died at
                # 82 seconds because it clicked an already-open tab four times.
                step.status = StepStatus.SUCCEEDED
                state.budget.consecutive_failures = 0
                state.note(
                    f"{tool.name} repeats without changing anything; treating it as already done."
                )
                return
            self._fail_step(
                state,
                step,
                f"{tool.name} has now produced an identical result "
                f"{self._action_counts[outcome]} times without advancing the objective",
                kind="internal",
            )
            state.note(f"Loop guard stopped a repeating action ({tool.name}).")
            return

        if observation.ok:
            step.status = StepStatus.SUCCEEDED
            state.budget.consecutive_failures = 0
            return

        step.status = StepStatus.FAILED
        # "It is not there" from a read-only lookup is an answer, not a
        # malfunction. The step still fails so the adapt phase can decide what to
        # do, but it must not push the run toward the abort threshold -- three
        # informative misses are not a system falling over.
        if not (observation.error_kind in _INFORMATIONAL and tool.risk is RiskTier.READ):
            state.budget.consecutive_failures += 1

        # 5. cheap recovery first, model only when it is actually needed.
        #
        # An in-place retry is only safe for an idempotent action. Re-firing a
        # non-idempotent one risks double-executing work the first attempt may
        # have partially completed -- approving an invoice twice, say -- so those
        # go to the adapt phase, which can re-establish preconditions and decide
        # whether repeating is actually safe.
        if observation.retryable and step.attempts <= MAX_IN_PLACE_RETRIES and tool.idempotent:
            delay = min(8.0, 1.5 * (2 ** (step.attempts - 1)))
            self._emit(
                state,
                "step.retrying",
                {"attempt": step.attempts, "after_seconds": delay, "kind": observation.error_kind},
                step_id=step.id,
            )
            await asyncio.sleep(delay)
            step.status = StepStatus.PENDING
            return

        if observation.error_kind == "invalid_args" and step.attempts <= 2:
            repaired = await self._repair_args(
                state, step, problem=observation.error or observation.summary
            )
            if repaired is not None:
                step.args = repaired
                step.status = StepStatus.PENDING
                return

        await self._adapt(state, step)

    async def _adapt(self, state: RunState, step: Step) -> None:
        """Decide what to do about a failure the cheap paths could not fix."""
        excerpts = self._recall(
            f"{step.intent} {step.observation.error if step.observation else ''}", limit=4
        )
        raw = await self._ask(
            state,
            prompts.adapt_prompt(
                state, step, catalog=self.registry.render_catalog(), memory_excerpts=excerpts
            ),
            prompts.ADAPT_SCHEMA,
            max_tokens=3000,
        )

        action = str(raw.get("action", "replan")).strip()
        reason = str(raw.get("reason", ""))
        analysis = str(raw.get("analysis", ""))
        self._emit(
            state,
            "adapt.decided",
            {"action": action, "reason": reason, "analysis": analysis},
            step_id=step.id,
        )
        state.note(f"{step.id} failed -> {action}: {reason[:200]}")

        if action == "retry" and step.attempts <= MAX_IN_PLACE_RETRIES:
            step.status = StepStatus.PENDING
            return

        if action == "repair_args":
            args = _parse_args_json(raw.get("new_args_json"))
            if args:
                step.args = args
                step.status = StepStatus.PENDING
                return

        if action == "skip":
            step.status = StepStatus.SKIPPED
            return

        if action == "insert_steps":
            inserted: list[Step] = []
            existing = {s.id for s in state.plan.steps}
            for index, item in enumerate(raw.get("steps") or [], start=1):
                new_step = _step_from(item, fallback_id=f"{step.id}-fix{index}")
                if new_step is None:
                    continue
                while new_step.id in existing:
                    new_step.id = f"{new_step.id}b"
                existing.add(new_step.id)
                new_step.origin = "repair"
                inserted.append(new_step)
            if inserted:
                position = state.plan.steps.index(step) + 1
                state.plan.steps[position:position] = inserted
                # Re-arm the failed step to run after its prerequisites.
                step.status = StepStatus.PENDING
                step.depends_on = list(dict.fromkeys([*step.depends_on, *(s.id for s in inserted)]))
                self._emit(
                    state,
                    "plan.steps_inserted",
                    {"steps": [{"id": s.id, "tool": s.tool, "intent": s.intent} for s in inserted]},
                    step_id=step.id,
                )
                return

        if action == "escalate":
            self._request_human(
                state,
                kind="approval",
                prompt=str(raw.get("escalation") or analysis or f"{step.intent} could not be completed."),
                context={"step": step.id, "analysis": analysis, "tool": step.tool},
                step_id=None,  # the run continues on other work after the answer
                options=["proceed", "hold"],
                origin="tool",
            )

        if action == "abort":
            state.error = f"aborted after {step.id}: {reason}"
            self._emit(state, "run.aborted", {"reason": reason})
            raise _Abort(reason)

        # Default and explicit `replan`: rebuild the remaining plan.
        if state.budget.replans_used < state.budget.max_replans:
            await self._plan(
                state,
                revision_reason=(
                    f"Step {step.id} ({step.tool}) failed: "
                    f"{step.observation.error if step.observation else 'unknown'}. "
                    f"Analysis: {analysis}"
                ),
            )
        else:
            state.note("Replan budget exhausted; continuing with the remaining plan.")

    # -- phase: verify ------------------------------------------------------
    async def _verify(self, state: RunState) -> Any:
        state.status = RunStatus.VERIFYING
        self._emit(state, "verification.started", {})

        verifier = Verifier(
            llm=self.llm,
            registry=self.registry,
            run_probe=lambda name, args: self._run_probe(state, name, args),
            on_llm_call=lambda: _bump(state),
        )
        verdict = await verifier.verify(state)
        state.verdict = verdict
        self._verification_evidence = verifier.probe_report

        self._emit(
            state,
            "verification.completed",
            {
                "complete": verdict.complete,
                "confidence": verdict.confidence,
                "summary": verdict.summary,
                "criteria": [
                    {"id": c.id, "status": c.status.value, "note": c.note} for c in verdict.criteria
                ],
                "unmet": verdict.unmet,
                "remediation": verdict.remediation,
            },
        )
        self._save(state)
        return verdict

    async def _run_probe(self, state: RunState, name: str, args: dict[str, Any]) -> Observation:
        """Execute one verification probe. Read-only is enforced here as well."""
        if not self.registry.has(name):
            return Observation(ok=False, summary=f"no such tool {name!r}", error_kind="not_found")
        tool = self.registry.get(name)
        if tool.risk is not RiskTier.READ:
            return Observation(
                ok=False,
                summary=f"{name} is not read-only and cannot be used for verification",
                error_kind="permission_denied",
            )
        state.budget.steps_used += 1
        observation = await tool.invoke(args, self._context(state))
        self._emit(
            state,
            "verification.probe",
            {"tool": name, "args": args, "ok": observation.ok, "summary": observation.brief(1200)},
        )
        return observation

    # -- phase: complete ----------------------------------------------------
    async def _complete(self, state: RunState) -> None:
        verdict = state.verdict
        evidence = getattr(self, "_verification_evidence", "")

        try:
            raw = await self._ask(
                state,
                prompts.report_prompt(state, probe_report=evidence),
                prompts.REPORT_SCHEMA,
                max_tokens=3000,
            )
        except LLMError as exc:
            raw = {
                "headline": "Run finished, but the closing report could not be generated.",
                "narrative": f"Report generation failed: {exc}",
                "needs_attention": [],
            }

        state.facts["report"] = {
            "headline": str(raw.get("headline", "")),
            "narrative": str(raw.get("narrative", "")),
            "needs_attention": [str(x) for x in (raw.get("needs_attention") or [])],
            "learnings": [str(x) for x in (raw.get("learnings") or [])],
        }

        if verdict and verdict.complete:
            state.status = RunStatus.COMPLETED
        elif state.completed_steps():
            state.status = RunStatus.PARTIAL
        else:
            state.status = RunStatus.FAILED
        state.finished_at = time.time()

        bundle = write_evidence_bundle(
            state,
            events=self.events.events(state.run_id),
            verification_evidence=evidence,
            artifacts_dir=self.settings.artifacts_dir,
        )
        state.facts["evidence_bundle"] = str(bundle)

        self._remember_run(state)
        self._emit(
            state,
            "run.completed",
            {
                "status": state.status.value,
                "headline": state.facts["report"]["headline"],
                "evidence_bundle": str(bundle),
            },
        )
        self._save(state)

    def _remember_run(self, state: RunState) -> None:
        """Write what was learned into episodic memory for the next run."""
        report = state.facts.get("report") or {}
        learnings = report.get("learnings") or []
        results = state.facts.get("results") or {}
        body = [
            f"Goal: {state.goal}",
            f"Outcome: {state.status.value}. {report.get('headline', '')}",
            "",
            "Per-item outcomes:",
            *(f"- {r.get('item')}: {r.get('status')} -- {r.get('detail')}" for r in results.values()),
        ]
        if learnings:
            body += ["", "Learned:", *(f"- {item}" for item in learnings)]
        self.memory.remember(
            f"Run {state.run_id}: {state.objective[:90]}",
            "\n".join(body),
            kind=EPISODIC,
            tags=["run", state.status.value],
        )

    # -- helpers ------------------------------------------------------------
    async def _repair_args(self, state: RunState, step: Step, *, problem: str) -> dict[str, Any] | None:
        if not self.registry.has(step.tool):
            return None
        entry = json.dumps(self.registry.get(step.tool).catalog_entry(), indent=2, ensure_ascii=False)
        try:
            raw = await self._ask(
                state,
                prompts.repair_prompt(step, problem=problem, catalog_entry=entry, state=state),
                prompts.REPAIR_SCHEMA,
                max_tokens=1500,
            )
        except LLMError:
            return None
        args = _parse_args_json(raw.get("args_json"))
        if not args:
            return None
        try:
            resolved = resolve_refs(args, build_ref_context(state))
        except UnresolvedReference:
            return None
        self._emit(
            state,
            "step.args_repaired",
            {"reason": str(raw.get("reason", "")), "args": resolved},
            step_id=step.id,
        )
        return resolved

    async def _ask(
        self, state: RunState, prompt: str, schema: dict, *, max_tokens: int = 4000
    ) -> dict[str, Any]:
        exhausted = state.budget.exceeded()
        if exhausted and "model-call" in exhausted:
            raise LLMError(f"model call budget exhausted ({exhausted})")
        _bump(state)
        result = await self.llm.complete_json(
            system=prompts.OPERATOR_IDENTITY.format(company=self.company_name),
            messages=[Message("user", prompt)],
            schema=schema,
            temperature=0.1,
            max_tokens=max_tokens,
        )
        return result if isinstance(result, dict) else {}

    def _recall(self, query: str, *, limit: int = 5) -> str:
        documents = self.memory.search(query, limit=limit)
        return "\n\n".join(document.render() for document in documents)

    def _context(self, state: RunState) -> ToolContext:
        if self._ctx is None or self._ctx.run_id != state.run_id:
            self._ctx = ToolContext(
                settings=self.settings,
                run_id=state.run_id,
                events=self.events,
                state=state,
                memory=self.memory,
                artifacts_dir=self.settings.artifacts_dir,
            )
        else:
            self._ctx.state = state
        return self._ctx

    async def _teardown(self) -> None:
        if self._ctx is None:
            return
        session = self._ctx.resources.pop("browser", None)
        if session is not None:
            await session.close()
        desktop = self._ctx.resources.pop("desktop", None)
        if desktop is not None:
            await desktop.close()
        client = self._ctx.resources.pop("http_client", None)
        if client is not None:
            await client.aclose()

    def _request_human(
        self,
        state: RunState,
        *,
        kind: str,
        prompt: str,
        context: dict[str, Any],
        step_id: str | None,
        options: list[str] | None = None,
        risk: RiskTier | None = None,
        origin: str = "policy",
    ) -> None:
        request = HumanRequest(
            id=f"hr{len(state.human_requests) + 1}",
            kind="approval" if kind == "approval" else "question",
            origin=origin,  # type: ignore[arg-type]
            step_id=step_id,
            prompt=prompt,
            context=context,
            options=options or [],
            risk=risk,
        )
        state.human_requests.append(request)
        state.status = RunStatus.AWAITING_HUMAN
        self._emit(
            state,
            "human.requested",
            {
                "request_id": request.id,
                "kind": request.kind,
                "prompt": prompt,
                "options": request.options,
                "context": context,
            },
            step_id=step_id,
        )
        self._save(state)
        raise _Paused()

    def _fail_step(self, state: RunState, step: Step, message: str, *, kind: str) -> None:
        step.status = StepStatus.FAILED
        step.ended_at = time.time()
        step.observation = Observation(
            ok=False, summary=message, error=message, error_kind=kind, retryable=False
        )
        state.budget.consecutive_failures += 1
        self._emit(state, "step.failed", {"error": message, "kind": kind}, step_id=step.id)

    def _emit(
        self, state: RunState, type: str, payload: dict[str, Any], *, step_id: str | None = None
    ) -> None:
        self.events.append(state.run_id, type, payload, step_id=step_id)

    def _save(self, state: RunState) -> None:
        self.events.save_snapshot(
            state.run_id,
            status=state.status.value,
            goal=state.goal,
            state=state.model_dump(mode="json"),
        )


class _Abort(Exception):
    """The objective cannot be achieved; stop without pretending otherwise."""


#: Affirmative words accepted when a human resumes without an explicit flag.
_AFFIRMATIVE = {
    "y", "yes", "ok", "okay", "approve", "approved", "go", "proceed",
    "confirm", "confirmed", "granted", "agree", "agreed", "sign off", "signed off",
}


def _reads_as_approval(response: str) -> bool:
    """Interpret a free-text resume as approval, failing closed.

    Only a clearly affirmative answer counts. Anything ambiguous -- including an
    empty response, or prose that merely *mentions* approval -- is treated as not
    approved, because the cost of misreading "I would not approve this" as
    consent is a payment nobody authorised.
    """
    text = response.strip().lower().rstrip(".!")
    if not text:
        return False
    if text in _AFFIRMATIVE:
        return True
    first = text.split()[0].strip(",;:")
    negations = {"no", "not", "don't", "dont", "never", "reject", "rejected", "decline", "declined", "hold"}
    if first in negations or any(f" {word} " in f" {text} " for word in ("do not", "cannot", "won't")):
        return False
    return first in _AFFIRMATIVE


def _bump(state: RunState) -> None:
    state.budget.llm_calls_used += 1


def _budget_from(settings: Settings):
    from .state import Budget

    return Budget(
        max_steps=settings.budget.max_steps,
        max_llm_calls=settings.budget.max_llm_calls,
        max_consecutive_failures=settings.budget.max_consecutive_failures,
        max_replans=settings.budget.max_replans,
        max_plan_cycles=settings.budget.max_plan_cycles,
        wall_clock_seconds=settings.budget.wall_clock_seconds,
    )


def _step_from(item: dict[str, Any], *, fallback_id: str) -> Step | None:
    tool = str(item.get("tool", "")).strip()
    if not tool:
        return None
    return Step(
        id=str(item.get("id") or fallback_id).strip() or fallback_id,
        intent=str(item.get("intent", "")).strip(),
        tool=tool,
        args=_parse_args_json(item.get("args_json")),
        depends_on=[str(d) for d in (item.get("depends_on") or [])],
        expect=str(item.get("expect", "")),
    )


def _parse_args_json(raw: Any) -> dict[str, Any]:
    """Parse the args-as-JSON-string convention the schemas use.

    Arguments travel as a *string* rather than a nested object because an open
    object is exactly what schema-constrained decoding handles worst -- Gemini
    rejects a property-less object outright, and other providers fill it with
    invented keys. A string the kernel parses is stricter in practice.
    """
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    from ..llm.base import extract_json

    try:
        parsed = extract_json(str(raw))
    except LLMError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def desktop_surface_available() -> bool:
    """Can this machine actually drive a desktop application?

    Checked explicitly rather than by catching ImportError around the module.
    ``desktop_tools`` imports pywinauto *lazily*, inside the functions that use
    it, so the module imports cleanly on Linux and an ImportError guard never
    fires. The tools would then be advertised to the planner and fail only when
    called -- the worst outcome, because the model wastes a step and a recovery
    cycle discovering that a capability it was offered does not exist.

    An operator should present the surfaces it genuinely has.
    """
    import importlib.util
    import sys

    if sys.platform != "win32":
        return False  # UI Automation is a Windows API
    return all(importlib.util.find_spec(m) is not None for m in ("pywinauto", "PySide6"))


def build_registry(settings: Settings) -> ToolRegistry:
    """Assemble every tool: built-in surfaces plus declarative connectors.

    The connector files are loaded last and are not special-cased -- a YAML-defined
    tool is indistinguishable from a Python one to the planner, which is the point.
    """
    from ..tools.browser_tools import BROWSER_TOOLS
    from ..tools.connector import load_connectors
    from ..tools.core_tools import CORE_TOOLS
    from ..tools.drive_tools import DRIVE_TOOLS

    tools = [*DRIVE_TOOLS, *BROWSER_TOOLS, *CORE_TOOLS]

    if desktop_surface_available():
        from ..tools.desktop_tools import DESKTOP_TOOLS

        tools.extend(DESKTOP_TOOLS)

    registry = ToolRegistry(tools)
    for tool in load_connectors(settings.company_dir / "connectors"):
        registry.register(tool)
    return registry
