"""The operator's own faculties: recall, working memory, and reaching a person.

These are not integrations. They are the things that make the agent an operator
rather than a script: it can look up how this company does something, write down
what it has concluded so a later step can rely on it, and stop to ask when
stopping is the right answer.

``ask_human`` and ``escalate_to_human`` exist as *tools* rather than as a kernel
behaviour so that asking is a planned, auditable action the model can choose --
including mid-plan, when it discovers something the plan did not anticipate. The
kernel also forces a pause when policy demands approval; the two paths converge
on the same :class:`~centralign.runtime.state.HumanRequest`.
"""

from __future__ import annotations

from typing import Any

from ..memory.store import CORRECTIVE, EPISODIC, SEMANTIC
from ..runtime.state import Observation, RiskTier
from .base import FailureKind, HumanInputRequired, Tool, ToolContext, ToolFailure


class SearchCompanyMemory(Tool):
    name = "company_memory_search"
    surface = "memory"
    description = (
        "Search company memory for the procedures, policies, thresholds and system "
        "details that govern this kind of work. Covers written SOPs and policies, "
        "notes from previous runs, and corrections a person has given before. "
        "Search this before assuming how something should be done."
    )
    args_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keywords, including any identifiers (vendor, PO, policy code).",
            },
            "limit": {"type": "integer", "description": "Maximum documents to return (default 5)."},
        },
        "required": ["query"],
    }
    risk = RiskTier.READ

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        limit = int(args.get("limit") or 5)
        documents = ctx.memory.search(
            str(args["query"]), kinds=[SEMANTIC, EPISODIC, CORRECTIVE], limit=max(1, min(limit, 10))
        )
        if not documents:
            return Observation(
                ok=True,
                summary=(
                    f"No company memory matched {args['query']!r}. "
                    "Proceed on general knowledge, and state that as an assumption."
                ),
                data={"hits": 0, "documents": []},
            )
        body = "\n\n".join(document.render() for document in documents)
        return Observation(
            ok=True,
            summary=f"{len(documents)} document(s) from company memory:\n\n{body}",
            data={
                "hits": len(documents),
                "documents": [
                    {"id": d.id, "title": d.title, "kind": d.kind, "source": d.source}
                    for d in documents
                ],
            },
        )


class NoteFact(Tool):
    name = "note_fact"
    surface = "memory"
    description = (
        "Write a named value onto the run's working memory so later steps can use it "
        "by reference as ${facts.<key>}. Use this for anything extracted or concluded "
        "that a later step depends on, such as the data read off a document."
    )
    args_schema = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Short identifier, e.g. 'invoice_INV_HL_2207'."},
            "value": {"description": "Any JSON value: string, number, object or array."},
            "note": {"type": "string", "description": "Optional one-line explanation."},
        },
        "required": ["key", "value"],
    }
    risk = RiskTier.READ

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        key = str(args["key"]).strip()
        if not key:
            raise ToolFailure("'key' must not be empty", kind=FailureKind.INVALID_ARGS)
        ctx.state.facts[key] = args["value"]
        return Observation(
            ok=True,
            summary=f"Recorded fact '{key}'. Reference it later as ${{facts.{key}}}.",
            data={"key": key, "value": args["value"], "note": args.get("note", "")},
        )


class RecordResult(Tool):
    name = "record_result"
    surface = "memory"
    description = (
        "Record the outcome for one work item, with the reason and any evidence. "
        "Call this once per item as you finish it -- including items you deliberately "
        "did not action. These records are what the final report and verification are "
        "built from, so an item without one counts as unaccounted for."
    )
    args_schema = {
        "type": "object",
        "properties": {
            "item": {"type": "string", "description": "Identifier of the work item, e.g. an invoice number."},
            "status": {
                "type": "string",
                "enum": ["completed", "blocked", "skipped", "escalated", "failed"],
                "description": "What happened to this item.",
            },
            "detail": {"type": "string", "description": "Why, in one or two sentences."},
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Supporting references: record ids, screenshot paths, ledger ids.",
            },
        },
        "required": ["item", "status", "detail"],
    }
    risk = RiskTier.READ

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        results: dict[str, Any] = ctx.state.facts.setdefault("results", {})
        record = {
            "item": str(args["item"]).strip(),
            "status": str(args["status"]).strip(),
            "detail": str(args["detail"]).strip(),
            "evidence": list(args.get("evidence") or []),
        }
        results[record["item"]] = record
        return Observation(
            ok=True,
            summary=f"Recorded outcome for {record['item']}: {record['status']} -- {record['detail']}",
            data=record,
        )


class AskHuman(Tool):
    name = "ask_human"
    surface = "human"
    description = (
        "Pause the run and ask a person a question, when the information genuinely "
        "cannot be obtained from the available systems or company memory. The run "
        "suspends and resumes with the answer. Do not use this for anything you can "
        "find out yourself."
    )
    args_schema = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question, specific and answerable."},
            "why": {"type": "string", "description": "Why this cannot be resolved without a person."},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional suggested answers.",
            },
        },
        "required": ["question", "why"],
    }
    risk = RiskTier.READ

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        raise HumanInputRequired(
            str(args["question"]),
            kind="question",
            options=list(args.get("options") or []),
            context={"why": str(args.get("why", ""))},
        )


class EscalateToHuman(Tool):
    name = "escalate_to_human"
    surface = "human"
    description = (
        "Hand a decision to a person, with your analysis and recommendation. Use this "
        "when you have established the facts but the decision is not yours to make -- "
        "a discrepancy that needs a judgement call, or an action beyond your authority. "
        "State what you found and what you advise; the person decides."
    )
    args_schema = {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "What the decision concerns."},
            "findings": {"type": "string", "description": "The facts you established, with figures."},
            "recommendation": {"type": "string", "description": "What you advise, and why."},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The decisions available to the person.",
            },
        },
        "required": ["subject", "findings", "recommendation"],
    }
    risk = RiskTier.READ

    def preview(self, args: dict[str, Any]) -> str:
        return f"Escalate to a person: {args.get('subject')}"

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        raise HumanInputRequired(
            f"{args['subject']}\n\nFindings: {args['findings']}\n\n"
            f"Recommendation: {args['recommendation']}",
            kind="approval",
            options=list(args.get("options") or ["approve", "reject"]),
            context={
                "subject": str(args["subject"]),
                "findings": str(args["findings"]),
                "recommendation": str(args["recommendation"]),
            },
        )


CORE_TOOLS = [
    SearchCompanyMemory(),
    NoteFact(),
    RecordResult(),
    AskHuman(),
    EscalateToHuman(),
]
