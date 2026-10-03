"""Drive the full system with a recorded decision sequence -- no API key needed.

**What this is, precisely.** Everything here is real except the model's
judgement: real Chromium driving the real ERP through its forms, real HTTP to the
connector-defined API, real PDF extraction, the real policy engine, real
verification probes against the systems of record, and a real evidence bundle.
The *decisions* come from a script rather than from a model.

So this demonstrates the operator's hands, not its head. It exists because a
reviewer should be able to inspect a genuine evidence bundle, watch the approval
gate actually block an action, and see the ERP refuse an over-authority approval,
without first obtaining an API key. For autonomy -- the system working out what to
do for itself -- run it live:

    cli run "The vendor invoices in the shared drive need processing."

Phase 1 is the sequence asserted by ``tests/test_full_workflow.py``, imported
rather than copied so the demo and the test cannot drift apart. Phase 2 adds the
human-in-the-loop path: an invoice above the clerk's authority, where the ERP
refuses, policy stops the operator, and granting approval lets it assume the
controller identity and finish.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from centralign.config import SETTINGS, Budget  # noqa: E402
from centralign.events.log import EventLog  # noqa: E402
from centralign.llm.replay import ScriptedProvider  # noqa: E402
from centralign.memory.store import MemoryStore  # noqa: E402
from centralign.policy.guard import PolicyGuard  # noqa: E402
from centralign.runtime.kernel import Kernel, build_registry  # noqa: E402
from centralign.cli import _print_event, _summarise, console  # noqa: E402

from tests.conftest import intake, plan, probes, report, step, verdict  # noqa: E402
from tests.test_full_workflow import (  # noqa: E402
    CRITERIA,
    GL_LOGISTICS,
    VERIFICATION_PROBES,
    _clean_invoice_steps,
    _over_billed_invoice_steps,
)

GL_COMPONENTS = "5010-RAW-MATERIALS"


def _northwind_steps() -> list[dict]:
    """INR 128,400 -- clean match, but above the clerk's INR 50,000 authority."""
    return [
        step("company_memory_search", id="n1", intent="check the approval authority limits", args={"query": "approval authority limit controller"}),
        step("drive_read", id="n2", intent="read the Northwind invoice and extract its details", args={"path": "invoices-inbox/INV-NW-8841.pdf"}),
        step("vendor_lookup", id="n3", intent="confirm Northwind is an approved vendor", args={"name": "Northwind Components Pvt Ltd"}),
        step("erp_login", id="n4", intent="sign in to Nexus ERP as the AP clerk", args={"account": "ap_clerk"}),
        step("browser_open", id="n5", intent="check the remaining balance on PO-4471", args={"path": "/purchase-orders/PO-4471"}),
        step("browser_open", id="n6", intent="open the invoice registration form", args={"path": "/invoices/new"}),
        step(
            "browser_fill",
            id="n7",
            intent="enter the Northwind invoice details",
            args={
                "fields": {
                    "Invoice number": "INV-NW-8841",
                    "Vendor name": "Northwind Components Pvt Ltd",
                    "Purchase order number": "PO-4471",
                    "Invoice amount": "128400",
                    "Invoice date": "2026-09-28",
                    "GL code": GL_COMPONENTS,
                }
            },
        ),
        step("browser_click", id="n8", intent="register the invoice in Nexus ERP", args={"target": "Register invoice"}),
        # The clerk attempt is left in deliberately: the ERP refuses it, and that
        # refusal appears in the stream as an observation rather than a crash.
        step("browser_click", id="n9", intent="attempt approval as the clerk", args={"target": "Approve invoice"}),
        # Policy stops the run here and asks a person.
        step("erp_login", id="n10", intent="switch to the Finance Controller account", args={"account": "finance_controller"}, depends_on=["n9"]),
        step("browser_open", id="n11", intent="reopen the registered invoice", args={"path": "${steps.n8.data.url}"}, depends_on=["n10"]),
        step("browser_click", id="n12", intent="approve the invoice on the controller account", args={"target": "Approve invoice"}, depends_on=["n11"]),
        step(
            "ledger_record",
            id="n13",
            intent="post the approved invoice to the general ledger",
            args={
                "invoice_number": "INV-NW-8841",
                "vendor": "Northwind Components Pvt Ltd",
                "po_number": "PO-4471",
                "amount": 128400,
                "currency": "INR",
                "gl_code": GL_COMPONENTS,
                "memo": "Matched PO-4471 in full. Approved by the Finance Controller under FIN-002.",
            },
            depends_on=["n12"],
        ),
        step(
            "drive_move",
            id="n14",
            intent="file the processed invoice",
            args={"path": "invoices-inbox/INV-NW-8841.pdf", "to_folder": "invoices-processed"},
            depends_on=["n13"],
        ),
        step(
            "record_result",
            id="n15",
            intent="record the outcome for INV-NW-8841",
            args={
                "item": "INV-NW-8841",
                "status": "completed",
                "detail": (
                    "Matched PO-4471 in full (INR 128,400). Beyond clerk authority, so "
                    "approved on the Finance Controller account after human sign-off."
                ),
                "evidence": ["PO-4471", "ledger"],
            },
            depends_on=["n14"],
        ),
    ]


def _build_kernel(responses, settings, memory, events) -> Kernel:
    return Kernel(
        settings=settings,
        llm=ScriptedProvider(list(responses)),
        registry=build_registry(settings),
        memory=memory,
        policy=PolicyGuard.from_file(settings.company_dir / "policies.yaml"),
        events=events,
    )


async def main() -> int:
    try:
        httpx.post(f"{SETTINGS.erp_url}/_sandbox/reset", timeout=15.0)
    except Exception:
        console.print("[red]The sandbox is not running. Start it with:[/]")
        console.print("  python -m sandbox.serve --reset")
        return 1

    settings = dataclasses.replace(
        SETTINGS,
        llm_cache=False,
        budget=Budget(max_steps=80, max_llm_calls=40, wall_clock_seconds=900),
    )
    settings.ensure_dirs()

    memory = MemoryStore(settings.memory_db)
    memory.ingest_company_dir(settings.company_dir)
    events = EventLog(settings.event_db)
    events.subscribe(_print_event)

    console.rule("[bold]Phase 1 — autonomous processing and a held exception")
    console.print(
        "[dim]One clean invoice processed end to end; one over-billed invoice held "
        "with the figures stated. No human needed.[/]\n"
    )
    kernel = _build_kernel(
        [
            intake("Process the vendor invoices in the inbox", CRITERIA),
            plan(*_clean_invoice_steps(), *_over_billed_invoice_steps()),
            VERIFICATION_PROBES,
            verdict(("ac1", "met"), ("ac2", "met"), ("ac3", "met"), complete=True),
            report("Approved and posted 1 invoice; held 1 for over-billing against its PO."),
        ],
        settings,
        memory,
        events,
    )
    first = await kernel.start(
        "The vendor invoices in the shared drive need processing - please take care of them."
    )
    _summarise(first)

    console.rule("[bold]Phase 2 — an approval the operator is not authorised to make")
    console.print(
        "[dim]INR 128,400 is above the clerk's limit. Watch the ERP refuse the clerk "
        "attempt, then watch policy stop the operator and ask a person.[/]\n"
    )
    kernel2 = _build_kernel(
        [
            intake(
                "Process invoice INV-NW-8841",
                [
                    ("ac1", "INV-NW-8841 is approved in Nexus ERP by an authorised approver"),
                    ("ac2", "INV-NW-8841 appears in the general ledger"),
                    ("ac3", "The source document is filed out of the inbox"),
                ],
            ),
            plan(*_northwind_steps()),
            probes(
                {"id": "p1", "criterion_id": "ac1", "tool": "browser_open",
                 "args": {"path": "/invoices?q=INV-NW-8841"},
                 "looking_for": "status approved and who approved it"},
                {"id": "p2", "criterion_id": "ac2", "tool": "ledger_list", "args": {},
                 "looking_for": "a ledger entry for INV-NW-8841"},
                {"id": "p3", "criterion_id": "ac3", "tool": "drive_list",
                 "args": {"folder": "invoices-processed"},
                 "looking_for": "the filed source document"},
            ),
            verdict(("ac1", "met"), ("ac2", "met"), ("ac3", "met"), complete=True),
            report("INV-NW-8841 approved on the controller account after sign-off, and posted."),
        ],
        settings,
        memory,
        events,
    )
    second = await kernel2.start("Process invoice INV-NW-8841 from the inbox.")
    _summarise(second)

    pending = second.pending_human()
    if pending is not None:
        console.rule("[bold yellow]The operator stopped and asked. Granting approval.")
        console.print(f"[yellow]{pending.prompt}[/]\n")
        second = await kernel2.resume(
            second.run_id,
            response=(
                "Approved. Three-way match confirmed against PO-4471 and the vendor is "
                "in the approved master. Proceed on the controller account."
            ),
            approved=True,
        )
        _summarise(second)

    console.rule("[bold]Resulting state of the sandbox company")
    world = httpx.get(f"{SETTINGS.erp_url}/_sandbox/state", timeout=15.0).json()
    for invoice in world["invoices"]:
        console.print(
            f"  {invoice['number']:<14} {invoice['status']:<11} "
            f"INR {int(invoice['amount']):>9,}  approved_by={invoice['approved_by'] or '-'}"
        )
    console.print()
    for po in world["purchase_orders"]:
        remaining = int(po["total"]) - int(po.get("billed", 0))
        console.print(
            f"  {po['number']}  ordered {int(po['total']):>9,}  "
            f"billed {int(po.get('billed', 0)):>9,}  remaining {remaining:>9,}"
        )
    console.print()
    for entry in world.get("ledger", []):
        console.print(
            f"  {entry['entry_id']}  {entry['invoice_number']:<14} "
            f"INR {int(entry['amount']):>9,}  {entry['gl_code']}"
        )
    console.print()
    for folder in ("invoices-inbox", "invoices-processed", "invoices-exceptions"):
        names = sorted(p.name for p in (settings.drive_dir / folder).glob("*"))
        console.print(f"  [bold]{folder}[/]: {', '.join(names) or '(empty)'}")

    console.print()
    for state in (first, second):
        bundle = state.facts.get("evidence_bundle")
        if bundle:
            console.print(f"[bold]evidence[/] {Path(bundle) / 'REPORT.md'}")

    memory.close()
    events.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
