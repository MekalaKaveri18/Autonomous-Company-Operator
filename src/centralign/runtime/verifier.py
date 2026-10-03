"""Independent verification.

The claim "the work was completed" is worth nothing if it comes from the thing
that did the work. Agents are consistently, confidently wrong about their own
success: a form submitted into a validation error reads as a successful click, a
ledger POST that 503'd reads as attempted, and the summary at the end says
"approved 5 invoices" because that was the plan.

So verification here does not read the execution log and ask the model whether it
seems fine. It:

1. takes the acceptance criteria fixed at intake, *before* anything was done and
   before any rationalisation was available;
2. asks for read-only probes that would reveal a failure -- chosen to look at the
   systems of record, not at the agent's own account of them;
3. executes those probes against the live world, through a registry **filtered in
   code** to read-only tools, so a verifier cannot repair what it is meant to be
   judging;
4. judges each criterion from probe output only, with ``unverifiable`` available
   so that "we could not tell" does not get rounded up to "met".

The verifier may conclude the run is incomplete and hand back remediation. The
kernel then gets one bounded chance to fix it and re-verify. That loop is what
turns verification from a report into a control.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from ..llm.base import LLMError, LLMProvider, Message
from ..tools.base import Tool
from ..tools.registry import ToolRegistry
from . import prompts
from .state import (
    AcceptanceCriterion,
    CriterionStatus,
    Observation,
    RiskTier,
    RunState,
    Verdict,
)

ProbeRunner = Callable[[str, dict[str, Any]], Awaitable[Observation]]

MAX_PROBES = 8


class Verifier:
    def __init__(
        self,
        *,
        llm: LLMProvider,
        registry: ToolRegistry,
        run_probe: ProbeRunner,
        on_llm_call: Callable[[], None] | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.run_probe = run_probe
        self.on_llm_call = on_llm_call or (lambda: None)
        self.probe_report: str = ""

    # -- read-only enforcement ---------------------------------------------
    def read_only_tools(self) -> list[Tool]:
        """Tools the verifier may use.

        Enforced structurally rather than by instruction. Telling a model "only
        use read-only tools" works most of the time, and the failure mode when it
        does not is a verifier that fixes the defect it was supposed to find and
        then reports success.
        """
        return [
            tool
            for tool in self.registry
            if tool.risk is RiskTier.READ and tool.surface != "human"
        ]

    def read_only_catalog(self) -> str:
        return ToolRegistry(self.read_only_tools()).render_catalog()

    async def verify(self, state: RunState) -> Verdict:
        if not state.acceptance_criteria:
            return Verdict(
                complete=False,
                confidence=0.0,
                summary="No acceptance criteria were established, so completion cannot be judged.",
            )

        observations = await self._gather_evidence(state)
        self.probe_report = _format_probe_report(observations)
        return await self._judge(state, self.probe_report)

    # -- step 1: decide what to look at, then look --------------------------
    async def _gather_evidence(self, state: RunState) -> list[dict[str, Any]]:
        allowed = {tool.name for tool in self.read_only_tools()}

        self.on_llm_call()
        try:
            plan = await self.llm.complete_json(
                system=prompts.OPERATOR_IDENTITY.format(company="CentrAlign"),
                messages=[
                    Message("user", prompts.probe_prompt(state, read_only_catalog=self.read_only_catalog()))
                ],
                schema=prompts.PROBE_SCHEMA,
                temperature=0.0,
                max_tokens=3000,
            )
        except LLMError as exc:
            return [{"error": f"could not plan verification probes: {exc}"}]

        results: list[dict[str, Any]] = []
        for raw in (plan.get("probes") or [])[:MAX_PROBES]:
            tool_name = str(raw.get("tool", "")).strip()
            record: dict[str, Any] = {
                "id": raw.get("id", f"p{len(results) + 1}"),
                "criterion_id": raw.get("criterion_id", ""),
                "tool": tool_name,
                "looking_for": raw.get("looking_for", ""),
            }

            if tool_name not in allowed:
                # Not a failure of the run -- a failure of the probe. Recorded so
                # the judging step knows this criterion was not actually observed.
                record["error"] = (
                    f"tool {tool_name!r} is not available for verification "
                    f"(read-only tools: {', '.join(sorted(allowed))})"
                )
                results.append(record)
                continue

            args = _parse_args(raw.get("args_json"))
            record["args"] = args
            observation = await self.run_probe(tool_name, args)
            record["ok"] = observation.ok
            record["observed"] = observation.brief(2500)
            results.append(record)

        return results

    # -- step 2: judge against the criteria --------------------------------
    async def _judge(self, state: RunState, probe_report: str) -> Verdict:
        self.on_llm_call()
        try:
            raw = await self.llm.complete_json(
                system=prompts.OPERATOR_IDENTITY.format(company="CentrAlign"),
                messages=[Message("user", prompts.verdict_prompt(state, probe_report=probe_report))],
                schema=prompts.VERDICT_SCHEMA,
                temperature=0.0,
                max_tokens=3000,
            )
        except LLMError as exc:
            return Verdict(
                complete=False,
                confidence=0.0,
                summary=f"Verification could not be completed: {exc}",
                criteria=list(state.acceptance_criteria),
                unmet=[c.id for c in state.acceptance_criteria],
            )

        by_id = {c.id: c for c in state.acceptance_criteria}
        judged: list[AcceptanceCriterion] = []
        for entry in raw.get("criteria") or []:
            criterion = by_id.get(str(entry.get("id", "")))
            if criterion is None:
                continue
            criterion.status = _as_status(entry.get("status"))
            criterion.note = str(entry.get("note", ""))
            judged.append(criterion)

        # A criterion the verifier simply did not mention has not been verified.
        # Silence must not read as success.
        for criterion in state.acceptance_criteria:
            if criterion not in judged:
                criterion.status = CriterionStatus.UNVERIFIABLE
                criterion.note = "The verification step returned no finding for this criterion."
                judged.append(criterion)

        unmet = [
            c.id for c in judged if c.status in {CriterionStatus.UNMET, CriterionStatus.UNVERIFIABLE}
        ]
        # The model's own `complete` flag is advisory; the criteria decide. This
        # guards the common failure where a model reports complete=true while
        # simultaneously marking a criterion unmet.
        complete = not unmet and bool(raw.get("complete", False))

        return Verdict(
            complete=complete,
            confidence=_clamp(raw.get("confidence", 0.0)),
            summary=str(raw.get("summary", "")),
            criteria=judged,
            unmet=unmet,
            remediation=[str(item) for item in (raw.get("remediation") or [])],
        )


def _format_probe_report(records: list[dict[str, Any]]) -> str:
    if not records:
        return "(no verification probes were run)"
    blocks: list[str] = []
    for record in records:
        header = f"### probe {record.get('id')} -> criterion {record.get('criterion_id') or '?'}"
        lines = [header, f"tool: {record.get('tool')}"]
        if record.get("args"):
            lines.append(f"args: {json.dumps(record['args'], ensure_ascii=False)[:400]}")
        if record.get("looking_for"):
            lines.append(f"looking for: {record['looking_for']}")
        if record.get("error"):
            lines.append(f"NOT RUN: {record['error']}")
        else:
            lines.append(f"succeeded: {record.get('ok')}")
            lines.append(f"observed:\n{record.get('observed', '')}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _as_status(value: Any) -> CriterionStatus:
    try:
        return CriterionStatus(str(value).lower())
    except ValueError:
        return CriterionStatus.UNVERIFIABLE


def _clamp(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0
