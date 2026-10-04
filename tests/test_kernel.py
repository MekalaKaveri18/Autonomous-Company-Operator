"""Kernel behaviour, driven by a scripted model against real tools.

Each test pins down one property the kernel must hold regardless of model:
recovery that does not waste model calls, refusals that are not retried,
approvals that genuinely block, verification that cannot be talked into a pass,
and loops that terminate.
"""

from __future__ import annotations

import json

import pytest

from centralign.policy.guard import PolicyGuard
from centralign.runtime.kernel import MAX_REPEATED_ACTIONS
from centralign.runtime.state import CriterionStatus, RunStatus, StepStatus

from .conftest import adapt, intake, plan, probes, repair, report, step, verdict


class TestHappyPath:
    async def test_completes_verifies_and_writes_evidence(self, make_kernel, settings):
        kernel, provider = make_kernel(
            [
                intake("Read the thing", [("ac1", "The thing was read")]),
                plan(step("fake_read", args={"note": "hello"})),
                probes({"tool": "fake_read", "criterion_id": "ac1"}),
                verdict(("ac1", "met"), complete=True),
                report("The thing was read."),
            ]
        )
        state = await kernel.start("read the thing")

        assert state.status is RunStatus.COMPLETED
        assert state.verdict is not None and state.verdict.complete
        assert state.acceptance_criteria[0].status is CriterionStatus.MET

        bundle = settings.artifacts_dir / state.run_id
        assert (bundle / "REPORT.md").exists()
        assert (bundle / "run.json").exists()
        assert (bundle / "events.jsonl").exists()
        assert (bundle / "verification.md").exists()
        assert "The thing was read." in (bundle / "REPORT.md").read_text(encoding="utf-8")

    async def test_every_model_call_is_counted_against_the_budget(self, make_kernel):
        kernel, provider = make_kernel(
            [
                intake(),
                plan(step("fake_read")),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "met")),
                report(),
            ]
        )
        state = await kernel.start("go")
        # intake, plan, probes, verdict, report = 5
        assert state.budget.llm_calls_used == 5
        assert provider.prompts and len(provider.prompts) == 5

    async def test_references_resolve_from_earlier_observations(self, make_kernel, tools):
        kernel, _ = make_kernel(
            [
                intake(),
                plan(
                    step("fake_read", id="s1", args={"note": "first"}),
                    step("fake_strict", id="s2", args={"target": "${steps.s1.data.note}"},
                         depends_on=["s1"]),
                ),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "met")),
                report(),
            ]
        )
        state = await kernel.start("go")

        assert state.status is RunStatus.COMPLETED
        # The reference must have been substituted before the tool saw it.
        assert tools["fake_strict"].calls == [{"target": "first"}]


class TestRecovery:
    async def test_transient_failure_retries_without_consulting_the_model(
        self, make_kernel, tools, event_types
    ):
        """A blip should cost a retry, not a reasoning cycle.

        If the kernel asked the model what to do about every 503, a flaky
        afternoon would exhaust a free-tier quota before any work got done.
        """
        kernel, provider = make_kernel(
            [
                intake(),
                plan(step("fake_flaky")),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "met")),
                report(),
            ]
        )
        state = await kernel.start("go")

        assert state.status is RunStatus.COMPLETED
        assert len(tools["fake_flaky"].calls) == 2, "should have retried exactly once"
        assert state.budget.llm_calls_used == 5, "no adapt call should have been made"
        assert "step.retrying" in event_types(kernel, state.run_id)
        assert "adapt.decided" not in event_types(kernel, state.run_id)

    async def test_a_refusal_is_not_retried_and_goes_to_adapt(
        self, make_kernel, tools, event_types
    ):
        kernel, _ = make_kernel(
            [
                intake(),
                plan(step("fake_missing")),
                adapt("skip"),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "unmet"), complete=False),
                report(),
            ]
        )
        state = await kernel.start("go")

        assert len(tools["fake_missing"].calls) == 1, "not_found must not be retried"
        assert state.plan.steps[0].status is StepStatus.SKIPPED
        assert "adapt.decided" in event_types(kernel, state.run_id)

    async def test_invalid_arguments_are_repaired_then_succeed(self, make_kernel, tools):
        kernel, _ = make_kernel(
            [
                intake(),
                plan(step("fake_strict", args={"wrong_name": "x"})),
                repair({"target": "corrected"}),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "met")),
                report(),
            ]
        )
        state = await kernel.start("go")

        assert state.status is RunStatus.COMPLETED
        assert tools["fake_strict"].calls == [{"target": "corrected"}]
        assert state.plan.steps[0].status is StepStatus.SUCCEEDED

    async def test_unresolvable_reference_is_repaired_before_the_tool_runs(
        self, make_kernel, tools
    ):
        kernel, _ = make_kernel(
            [
                intake(),
                plan(step("fake_strict", args={"target": "${facts.never_set}"})),
                repair({"target": "fallback"}),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "met")),
                report(),
            ]
        )
        state = await kernel.start("go")
        assert tools["fake_strict"].calls == [{"target": "fallback"}]

    async def test_adapt_can_insert_prerequisite_steps(self, make_kernel, tools, event_types):
        kernel, _ = make_kernel(
            [
                intake(),
                plan(step("fake_missing", id="s1")),
                adapt(
                    "insert_steps",
                    steps=[
                        {
                            "id": "fix1",
                            "intent": "establish the prerequisite",
                            "tool": "fake_read",
                            "args_json": "{}",
                        }
                    ],
                ),
                # s1 is re-armed and fails again; this time give up on it.
                adapt("skip"),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "unmet"), complete=False),
                report(),
            ]
        )
        state = await kernel.start("go")

        assert "plan.steps_inserted" in event_types(kernel, state.run_id)
        assert any(s.id == "fix1" and s.status is StepStatus.SUCCEEDED for s in state.plan.steps)

    async def test_dependents_of_a_failed_step_are_skipped_not_run(self, make_kernel, tools):
        kernel, _ = make_kernel(
            [
                intake(),
                plan(
                    step("fake_missing", id="s1"),
                    step("fake_write", id="s2", depends_on=["s1"]),
                ),
                adapt("skip"),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "unmet"), complete=False),
                report(),
            ]
        )
        state = await kernel.start("go")

        assert tools["fake_write"].calls == [], "a step whose dependency failed must not run"
        assert state.plan.by_id("s2").status is StepStatus.SKIPPED

    async def test_abort_still_produces_a_report_and_evidence(self, make_kernel, settings):
        kernel, _ = make_kernel(
            [
                intake(),
                plan(step("fake_missing")),
                adapt("abort"),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "unmet"), complete=False),
                report("Could not proceed."),
            ]
        )
        state = await kernel.start("go")

        assert state.status in {RunStatus.FAILED, RunStatus.PARTIAL}
        assert state.error and "aborted" in state.error
        assert (settings.artifacts_dir / state.run_id / "REPORT.md").exists()


class TestApprovalGate:
    async def test_sensitive_action_pauses_before_executing(self, make_kernel, tools):
        kernel, _ = make_kernel(
            [intake(), plan(step("fake_approve", args={"amount": 128400}))]
        )
        state = await kernel.start("approve the payment")

        assert state.status is RunStatus.AWAITING_HUMAN
        assert tools["fake_approve"].calls == [], "must not execute before approval"

        pending = state.pending_human()
        assert pending is not None
        assert pending.kind == "approval"
        # The prompt must describe the action in business terms, not as a call.
        assert "Approve a payment of 128400" in pending.prompt

    async def test_granting_approval_resumes_and_executes(self, make_kernel, tools):
        kernel, _ = make_kernel(
            [
                intake(),
                plan(step("fake_approve", args={"amount": 1000})),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "met")),
                report(),
            ]
        )
        state = await kernel.start("approve it")
        assert state.status is RunStatus.AWAITING_HUMAN

        resumed = await kernel.resume(
            state.run_id, response="Checked against the PO, go ahead.", approved=True
        )

        assert resumed.status is RunStatus.COMPLETED
        assert tools["fake_approve"].calls == [{"amount": 1000}]
        assert resumed.human_requests[0].approved is True

    async def test_declining_approval_never_executes_the_action(self, make_kernel, tools):
        kernel, _ = make_kernel(
            [
                intake(),
                plan(step("fake_approve", args={"amount": 1000})),
                adapt("skip"),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "unmet"), complete=False),
                report(),
            ]
        )
        state = await kernel.start("approve it")
        resumed = await kernel.resume(
            state.run_id, response="No -- the variance is unexplained.", approved=False
        )

        assert tools["fake_approve"].calls == [], "a declined action must never run"
        assert resumed.human_requests[0].approved is False
        assert resumed.status is not RunStatus.COMPLETED

    async def test_approval_is_spent_once_and_not_reused(self, make_kernel, tools):
        """A granted approval must not become a standing permission."""
        kernel, _ = make_kernel(
            [
                intake(),
                plan(
                    step("fake_approve", id="s1", args={"amount": 10}),
                    step("fake_approve", id="s2", args={"amount": 20}),
                ),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "met")),
                report(),
            ]
        )
        state = await kernel.start("approve both")
        state = await kernel.resume(state.run_id, response="ok", approved=True)

        # The second sensitive step must raise its own approval request.
        assert state.status is RunStatus.AWAITING_HUMAN
        assert len(tools["fake_approve"].calls) == 1
        assert len([r for r in state.human_requests if not r.resolved]) == 1

    async def test_supervised_autonomy_gates_ordinary_writes_too(self, make_kernel, tools):
        kernel, _ = make_kernel(
            [intake(), plan(step("fake_write"))],
            policy=PolicyGuard({"autonomy": "supervised"}),
        )
        state = await kernel.start("write something")

        assert state.status is RunStatus.AWAITING_HUMAN
        assert tools["fake_write"].calls == []

    async def test_high_autonomy_lets_a_sensitive_write_through(self, make_kernel, tools):
        kernel, _ = make_kernel(
            [
                intake(),
                plan(step("fake_approve", args={"amount": 5})),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "met")),
                report(),
            ],
            policy=PolicyGuard({"autonomy": "high"}),
        )
        state = await kernel.start("go")

        assert state.status is RunStatus.COMPLETED
        assert tools["fake_approve"].calls == [{"amount": 5}]

    async def test_a_denied_tool_is_never_invoked(self, make_kernel, tools, event_types):
        kernel, _ = make_kernel(
            [
                intake(),
                plan(step("fake_approve", args={"amount": 5})),
                adapt("skip"),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "unmet"), complete=False),
                report(),
            ],
            policy=PolicyGuard({"autonomy": "high", "denied_tools": ["fake_approve"]}),
        )
        state = await kernel.start("go")

        assert tools["fake_approve"].calls == []
        assert "policy.denied" in event_types(kernel, state.run_id)

    async def test_a_blocking_intake_question_pauses_before_planning(self, make_kernel):
        kernel, _ = make_kernel([intake(blocking_question="Which entity is this for?")])
        state = await kernel.start("do the ambiguous thing")

        assert state.status is RunStatus.AWAITING_HUMAN
        assert state.plan.steps == []
        assert state.pending_human().kind == "question"

    async def test_an_answered_question_becomes_referenceable(self, make_kernel, tools):
        """A human's answer must be usable by later steps like any observation."""
        from centralign.tools.core_tools import AskHuman

        kernel, _ = make_kernel(
            [
                intake(),
                plan(
                    step("ask_human", id="s1", args={"question": "Which entity?", "why": "unstated"}),
                    step("fake_strict", id="s2", args={"target": "${steps.s1.data.response}"},
                         depends_on=["s1"]),
                ),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "met")),
                report(),
            ],
            extra_tools=[AskHuman()],
        )
        state = await kernel.start("go")
        assert state.status is RunStatus.AWAITING_HUMAN

        state = await kernel.resume(state.run_id, response="CentrAlign India")
        assert tools["fake_strict"].calls == [{"target": "CentrAlign India"}]


class TestVerification:
    async def test_a_write_tool_cannot_be_used_as_a_probe(self, make_kernel, tools):
        """Structural, not advisory: a verifier must not repair what it judges."""
        kernel, _ = make_kernel(
            [
                intake(),
                plan(step("fake_read")),
                probes({"tool": "fake_approve", "criterion_id": "ac1"}),
                verdict(("ac1", "unverifiable"), complete=False),
                report(),
            ]
        )
        state = await kernel.start("go")

        assert tools["fake_approve"].calls == [], "verification must never mutate state"
        evidence = (kernel.settings.artifacts_dir / state.run_id / "verification.md").read_text(
            encoding="utf-8"
        )
        assert "NOT RUN" in evidence

    async def test_an_unmet_criterion_overrides_a_claimed_completion(self, make_kernel):
        """The criteria decide, not the model's own summary flag."""
        kernel, _ = make_kernel(
            [
                intake("Do two things", [("ac1", "First done"), ("ac2", "Second done")]),
                plan(step("fake_read")),
                probes({"tool": "fake_read"}),
                # Model contradicts itself: complete=true but ac2 unmet.
                verdict(("ac1", "met"), ("ac2", "unmet"), complete=True),
                report(),
                # A remediation round follows, so the script must allow for it.
                plan(step("fake_read")),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "met"), ("ac2", "unmet"), complete=True),
                report(),
            ]
        )
        state = await kernel.start("go")

        assert state.verdict is not None
        assert state.verdict.complete is False
        assert "ac2" in state.verdict.unmet
        assert state.status is RunStatus.PARTIAL

    async def test_a_criterion_the_verifier_ignored_becomes_unverifiable(self, make_kernel):
        """Silence about a criterion must not read as success."""
        kernel, _ = make_kernel(
            [
                intake("Do two things", [("ac1", "First done"), ("ac2", "Second done")]),
                plan(step("fake_read")),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "met"), complete=True),  # ac2 simply not mentioned
                report(),
                plan(step("fake_read")),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "met"), complete=True),
                report(),
            ]
        )
        state = await kernel.start("go")

        ac2 = next(c for c in state.verdict.criteria if c.id == "ac2")
        assert ac2.status is CriterionStatus.UNVERIFIABLE
        assert state.verdict.complete is False

    async def test_failed_verification_triggers_one_remediation_round(
        self, make_kernel, event_types
    ):
        kernel, _ = make_kernel(
            [
                intake(),
                plan(step("fake_read")),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "unmet"), complete=False, remediation=["do the missing part"]),
                plan(step("fake_write")),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "met"), complete=True),
                report(),
            ]
        )
        state = await kernel.start("go")

        types = event_types(kernel, state.run_id)
        assert "verification.remediation_started" in types
        assert types.count("verification.completed") == 2
        assert state.status is RunStatus.COMPLETED

    async def test_remediation_is_bounded_to_avoid_an_endless_loop(self, make_kernel, event_types):
        kernel, _ = make_kernel(
            [
                intake(),
                plan(step("fake_read")),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "unmet"), complete=False, remediation=["try again"]),
                plan(step("fake_read")),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "unmet"), complete=False, remediation=["try again"]),
                report(),
            ]
        )
        state = await kernel.start("go")

        assert event_types(kernel, state.run_id).count("verification.completed") == 2
        assert state.status is RunStatus.PARTIAL


class TestTermination:
    async def test_the_loop_guard_stops_a_repeating_action(self, make_kernel, tools, event_types):
        """Budgets catch runaway cost; this catches a run that is busy but stuck."""
        repeated = [step("fake_read", id=f"s{i}", args={"note": "same"}) for i in range(1, 7)]
        kernel, _ = make_kernel(
            [
                intake(),
                plan(*repeated),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "unmet"), complete=False),
                report(),
            ]
        )
        state = await kernel.start("go")

        assert "loop.detected" in event_types(kernel, state.run_id)
        assert len(tools["fake_read"].calls) <= 5

    async def test_repeating_an_action_that_makes_progress_is_not_a_loop(
        self, make_kernel, tools, event_types
    ):
        """The guard must measure progress, not repetition.

        An earlier version keyed only on (tool, args), so six legitimate
        "Grant access" clicks -- identical arguments, different form contents, a
        different outcome each time -- were killed as a loop. Real work got
        cancelled by a safety mechanism, which is worse than the problem.
        """
        repeated = [step("fake_progress", id=f"g{i}") for i in range(1, 7)]
        kernel, _ = make_kernel(
            [
                intake(),
                plan(*repeated),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "met")),
                report(),
            ]
        )
        state = await kernel.start("go")

        assert "loop.detected" not in event_types(kernel, state.run_id)
        assert len(tools["fake_progress"].calls) == 6, "every step should have run"
        assert state.status is RunStatus.COMPLETED

    async def test_a_stuck_action_is_not_attempted_again_after_detection(
        self, make_kernel, tools
    ):
        """Detection is after the fact, so the next identical attempt is blocked."""
        repeated = [step("fake_read", id=f"s{i}", args={"note": "same"}) for i in range(1, 9)]
        kernel, _ = make_kernel(
            [
                intake(),
                plan(*repeated),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "unmet"), complete=False),
                report(),
            ]
        )
        state = await kernel.start("go")

        identical = [c for c in tools["fake_read"].calls if c.get("note") == "same"]
        assert len(identical) <= MAX_REPEATED_ACTIONS + 1, (
            f"kept going after detecting a loop: {len(identical)} identical calls"
        )

    async def test_benign_repetition_does_not_kill_the_run(
        self, make_kernel, tools, event_types
    ):
        """A harmless repeat must not cascade into a dead run.

        This is a real failure, not a hypothetical. A live onboarding run clicked
        an already-open tab four times; each click succeeded and left the world
        exactly as the plan wanted it. The guard failed the step anyway, its
        dependents were abandoned, the consecutive-failure budget tripped, and a
        healthy run died after 82 seconds. Repetition that keeps succeeding means
        the work is already done, not that the operator is stuck.
        """
        repeated = [step("fake_read", id=f"s{i}", args={"note": "same"}) for i in range(1, 7)]
        kernel, _ = make_kernel(
            [
                intake(),
                plan(*repeated, step("fake_write", id="after", depends_on=["s6"])),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "met")),
                report(),
            ]
        )
        state = await kernel.start("go")

        types = event_types(kernel, state.run_id)
        assert "loop.detected" in types, "the repetition should still be noticed"
        assert "budget.exceeded" not in types, "a benign repeat must not exhaust the budget"
        assert tools["fake_write"].calls == [{}], "dependent work must still run"
        assert state.status is RunStatus.COMPLETED
        assert not [s.id for s in state.plan.steps if s.status is StepStatus.FAILED]

    async def test_a_repeating_failure_still_stops_the_run(self, make_kernel, event_types):
        """The guard must keep its teeth for the case it was built for."""
        repeated = [step("fake_missing", id=f"m{i}") for i in range(1, 7)]
        kernel, _ = make_kernel(
            [
                intake(),
                plan(*repeated),
                *[adapt("retry") for _ in range(6)],
                probes({"tool": "fake_read"}),
                verdict(("ac1", "unmet"), complete=False),
                report(),
            ]
        )
        state = await kernel.start("go")
        assert state.status is not RunStatus.COMPLETED

    async def test_the_step_budget_terminates_a_long_plan(self, make_kernel, settings, event_types):
        import dataclasses

        from centralign.config import Budget

        settings_small = dataclasses.replace(settings, budget=Budget(max_steps=3, max_llm_calls=40))
        many = [step("fake_read", id=f"s{i}", args={"note": f"n{i}"}) for i in range(1, 10)]
        kernel, _ = make_kernel(
            [
                intake(),
                plan(*many),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "unmet"), complete=False),
                report(),
            ]
        )
        kernel.settings = settings_small
        # The budget is captured at start(), so patch the kernel's source of truth.
        state = await kernel.start("go")
        assert state.budget.steps_used <= state.budget.max_steps + 1


class TestAuditTrail:
    async def test_the_event_log_records_the_whole_decision_sequence(self, make_kernel, event_types):
        kernel, _ = make_kernel(
            [
                intake(),
                plan(step("fake_flaky")),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "met")),
                report(),
            ]
        )
        state = await kernel.start("go")

        types = event_types(kernel, state.run_id)
        for expected in (
            "run.created",
            "intake.started",
            "intake.completed",
            "plan.created",
            "step.started",
            "step.retrying",
            "step.observed",
            "verification.started",
            "verification.completed",
            "run.completed",
        ):
            assert expected in types, f"missing {expected} in {types}"

    async def test_events_are_strictly_ordered_and_contiguous(self, make_kernel):
        kernel, _ = make_kernel(
            [intake(), plan(step("fake_read")), probes({"tool": "fake_read"}), verdict(("ac1", "met")), report()]
        )
        state = await kernel.start("go")
        sequences = [event.seq for event in kernel.events.events(state.run_id)]
        assert sequences == list(range(1, len(sequences) + 1))

    async def test_a_run_can_be_reloaded_from_its_snapshot(self, make_kernel):
        kernel, _ = make_kernel(
            [intake(), plan(step("fake_read")), probes({"tool": "fake_read"}), verdict(("ac1", "met")), report()]
        )
        state = await kernel.start("go")

        from centralign.runtime.state import RunState

        reloaded = RunState.model_validate(kernel.events.load_snapshot(state.run_id))
        assert reloaded.run_id == state.run_id
        assert reloaded.status is state.status
        assert len(reloaded.plan.steps) == len(state.plan.steps)
        assert reloaded.verdict.complete == state.verdict.complete


class TestProgressivePlanning:
    """A plan running out is not the work being finished.

    This is the gap that made the first live run stop after its discovery pass:
    the model planned reads, the kernel saw an empty queue and went straight to
    verification. The planner must be asked to carry on.
    """

    async def test_an_exhausted_plan_asks_the_planner_to_continue(
        self, make_kernel, tools, event_types
    ):
        kernel, _ = make_kernel(
            [
                intake(),
                # Discovery only -- deliberately does not finish the job.
                plan(step("fake_read", id="s1", args={"note": "discover"}), complete=False),
                # Continuation: now do the actual work.
                plan(step("fake_write", id="s2"), complete=True),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "met")),
                report(),
            ]
        )
        state = await kernel.start("go")

        assert "plan.continuing" in event_types(kernel, state.run_id)
        assert tools["fake_write"].calls == [{}], "the continuation work must actually run"
        assert state.objective_complete is True
        assert state.status is RunStatus.COMPLETED

    async def test_continuation_does_not_spend_the_replan_budget(self, make_kernel):
        """Progressive planning is normal operation, not failure recovery."""
        kernel, _ = make_kernel(
            [
                intake(),
                plan(step("fake_read", id="s1"), complete=False),
                plan(step("fake_read", id="s2", args={"note": "more"}), complete=True),
                probes({"tool": "fake_read"}),
                verdict(("ac1", "met")),
                report(),
            ]
        )
        state = await kernel.start("go")
        assert state.budget.replans_used == 0
        assert state.budget.plan_cycles_used == 1

    async def test_continuation_is_bounded_so_the_run_terminates(self, make_kernel, settings):
        """A planner that never says 'done' must still stop."""
        import dataclasses

        from centralign.config import Budget

        never_done = [plan(step("fake_read", id=f"c{i}", args={"note": f"n{i}"}), complete=False)
                      for i in range(1, 12)]
        kernel, _ = make_kernel(
            [intake(), *never_done, probes({"tool": "fake_read"}), verdict(("ac1", "unmet"), complete=False), report()]
        )
        kernel.settings = dataclasses.replace(
            settings, budget=Budget(max_steps=60, max_llm_calls=60, max_plan_cycles=3)
        )
        state = await kernel.start("go")

        assert state.budget.plan_cycles_used <= state.budget.max_plan_cycles + 1
        assert state.status in {RunStatus.PARTIAL, RunStatus.FAILED}

    async def test_a_planner_reporting_completion_with_no_steps_ends_cleanly(self, make_kernel):
        kernel, _ = make_kernel(
            [
                intake(),
                plan(step("fake_read", id="s1"), complete=False),
                plan(complete=True),  # nothing left to do
                probes({"tool": "fake_read"}),
                verdict(("ac1", "met")),
                report(),
            ]
        )
        state = await kernel.start("go")
        assert state.status is RunStatus.COMPLETED
        assert state.objective_complete is True
