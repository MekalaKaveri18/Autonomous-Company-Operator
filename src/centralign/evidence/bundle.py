"""Evidence bundle: what a person receives when the run finishes.

The deliverable of an AI employee is not a chat message. It is the work, plus
enough evidence that a reviewer can confirm the work without redoing it. So every
run writes a directory:

    artifacts/<run_id>/
        REPORT.md         the human-readable account, verdict first
        run.json          the full final RunState
        events.jsonl      the complete append-only audit trail
        verification.md   the probe observations the verdict was based on
        shot-*.png        screenshots captured as each browser action happened

``REPORT.md`` leads with the verification verdict rather than the narrative,
deliberately. An operator's self-description is the least trustworthy thing in
the bundle and should not be the first thing read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from ..events.log import Event
from ..runtime.state import CriterionStatus, RunState, StepStatus

_STATUS_MARK = {
    CriterionStatus.MET: "MET",
    CriterionStatus.UNMET: "NOT MET",
    CriterionStatus.UNVERIFIABLE: "UNVERIFIED",
    CriterionStatus.UNKNOWN: "NOT CHECKED",
}


def write_evidence_bundle(
    state: RunState,
    *,
    events: Iterable[Event],
    verification_evidence: str,
    artifacts_dir: Path,
) -> Path:
    directory = artifacts_dir / state.run_id
    directory.mkdir(parents=True, exist_ok=True)

    (directory / "run.json").write_text(
        json.dumps(state.model_dump(mode="json"), indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    with (directory / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event.as_dict(), ensure_ascii=False, default=str) + "\n")

    (directory / "verification.md").write_text(
        f"# Verification evidence -- {state.run_id}\n\n"
        "These are the read-only observations the verdict was based on. They were\n"
        "taken from the systems after the work finished, not from the execution log.\n\n"
        f"{verification_evidence or '(no probes were run)'}\n",
        encoding="utf-8",
    )

    report = render_report(state, directory=directory)
    (directory / "REPORT.md").write_text(report, encoding="utf-8")
    return directory


def render_report(state: RunState, *, directory: Path | None = None) -> str:
    report = state.facts.get("report") or {}
    verdict = state.verdict
    lines: list[str] = []

    lines.append(f"# {report.get('headline') or state.objective or state.goal}")
    lines.append("")
    lines.append(f"**Run:** `{state.run_id}` · **Outcome:** {state.status.value}")
    if verdict:
        lines.append(
            f" · **Verified complete:** {'yes' if verdict.complete else 'no'} "
            f"(confidence {verdict.confidence:.0%})"
        )
    lines.append("")
    lines.append(f"> **Request:** {state.goal}")
    lines.append("")

    # Verdict first: the independent check outranks the agent's own account.
    lines.append("## Verification")
    lines.append("")
    if not verdict:
        lines.append("_The run did not reach verification._")
    else:
        if verdict.summary:
            lines.append(verdict.summary)
            lines.append("")
        lines.append("| Acceptance criterion | Result | Observed evidence |")
        lines.append("| --- | --- | --- |")
        for criterion in verdict.criteria:
            lines.append(
                f"| {_cell(criterion.text)} | **{_STATUS_MARK.get(criterion.status, '?')}** "
                f"| {_cell(criterion.note)} |"
            )
        lines.append("")
        if verdict.remediation:
            lines.append("**Still outstanding:**")
            lines += [f"- {item}" for item in verdict.remediation]
            lines.append("")

    lines.append("## What happened")
    lines.append("")
    lines.append(report.get("narrative") or "_No narrative was generated._")
    lines.append("")

    attention = report.get("needs_attention") or []
    if attention:
        lines.append("## Needs a person")
        lines.append("")
        lines += [f"- {item}" for item in attention]
        lines.append("")

    results = state.facts.get("results") or {}
    if results:
        lines.append("## Outcome per item")
        lines.append("")
        lines.append("| Item | Outcome | Reason | Evidence |")
        lines.append("| --- | --- | --- | --- |")
        for record in results.values():
            evidence = ", ".join(str(e) for e in (record.get("evidence") or [])) or "-"
            lines.append(
                f"| {_cell(record.get('item'))} | {_cell(record.get('status'))} "
                f"| {_cell(record.get('detail'))} | {_cell(evidence)} |"
            )
        lines.append("")

    if state.human_requests:
        lines.append("## Human involvement")
        lines.append("")
        for request in state.human_requests:
            outcome = (
                "approved" if request.approved
                else "declined" if request.approved is False
                else "answered" if request.resolved
                else "still waiting"
            )
            lines.append(f"- **{request.kind}** ({outcome}): {_cell(request.prompt, 400)}")
            if request.response:
                lines.append(f"  - response: {_cell(request.response, 300)}")
        lines.append("")

    lines.append("## Execution trace")
    lines.append("")
    lines.append("| # | Step | Tool | Status | Observation |")
    lines.append("| --- | --- | --- | --- | --- |")
    for index, step in enumerate(state.plan.steps, start=1):
        observation = step.observation.brief(220) if step.observation else ""
        lines.append(
            f"| {index} | {_cell(step.intent, 120)} | `{step.tool}` "
            f"| {_mark(step)} | {_cell(observation, 240)} |"
        )
    lines.append("")

    if state.assumptions:
        lines.append("## Assumptions made")
        lines.append("")
        lines += [f"- {item}" for item in state.assumptions]
        lines.append("")

    learnings = report.get("learnings") or []
    if learnings:
        lines.append("## Learned for next time")
        lines.append("")
        lines += [f"- {item}" for item in learnings]
        lines.append("")

    if directory is not None:
        shots = sorted(p.name for p in directory.glob("shot-*.png"))
        if shots:
            lines.append("## Screenshots")
            lines.append("")
            lines.append(
                f"{len(shots)} screenshot(s) captured as each browser action happened:"
            )
            lines.append("")
            lines += [f"- [{name}](./{name})" for name in shots]
            lines.append("")

    budget = state.budget
    lines.append("## Cost and limits")
    lines.append("")
    lines.append(
        f"- Steps executed: {budget.steps_used} of {budget.max_steps}\n"
        f"- Model calls: {budget.llm_calls_used} of {budget.max_llm_calls}\n"
        f"- Replans: {budget.replans_used} of {budget.max_replans}\n"
        f"- Plan revisions: {state.plan.version}\n"
        f"- Elapsed: {(state.finished_at or 0) - state.created_at:.0f}s"
    )
    if state.error:
        lines.append("")
        lines.append(f"**Run error:** {state.error}")

    return "\n".join(lines) + "\n"


_MARKS = {
    StepStatus.SUCCEEDED: "ok",
    StepStatus.FAILED: "**failed**",
    StepStatus.SKIPPED: "skipped",
    StepStatus.PENDING: "not run",
    StepStatus.RUNNING: "running",
    StepStatus.AWAITING_HUMAN: "awaiting human",
}


def _mark(step) -> str:
    mark = _MARKS.get(step.status, step.status.value)
    return f"{mark} ({step.attempts}x)" if step.attempts > 1 else mark


def _cell(value, limit: int = 180) -> str:
    """Make text safe for a markdown table cell."""
    text = str(value if value is not None else "").replace("\n", " ").replace("|", "\\|")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."
