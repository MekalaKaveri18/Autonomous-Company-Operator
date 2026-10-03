"""The whole system, end to end, against the real sandbox.

Only the model's *reasoning* is scripted. Everything else is real: Chromium
driving the real ERP through its forms, HTTP against the connector-defined API,
PDF text extraction off the drive, the policy engine, verification probes, and
the evidence bundle.

This is the test that would catch the failure mode the brief is really probing
for -- a system that produces a confident report while the systems of record are
untouched. Every assertion at the end reads the **sandbox state**, not the
operator's account of it. If the operator claimed success and nothing happened,
these fail.

The scripted plan is one plausible plan, not the only one. A live model will
produce a different sequence; what is fixed here is that *this* sequence, run for
real, leaves the world in the right state.
"""

from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from centralign.config import SETTINGS, Budget
from centralign.events.log import EventLog
from centralign.llm.replay import ScriptedProvider
from centralign.memory.store import MemoryStore
from centralign.policy.guard import PolicyGuard
from centralign.runtime.kernel import Kernel, build_registry
from centralign.runtime.state import CriterionStatus, RunStatus, StepStatus

from .conftest import intake, plan, probes, report, step, verdict


def _sandbox_up() -> bool:
    try:
        httpx.get(f"{SETTINGS.erp_url}/_sandbox/health", timeout=2.0)
        httpx.get(f"{SETTINGS.api_url}/health", timeout=2.0)
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _sandbox_up(), reason="sandbox not running -- start it with 'python -m sandbox.serve'"
)

GL_LOGISTICS = "5240-INBOUND-FREIGHT"

CRITERIA = [
    ("ac1", "Every document in the invoice inbox has a recorded outcome"),
    ("ac2", "No invoice was approved for more than its purchase order's remaining balance"),
    ("ac3", "Every approved invoice appears in the general ledger"),
]


def _clean_invoice_steps() -> list[dict]:
    """Helios: a clean three-way match below the clerk's authority."""
    return [
        step("company_memory_search", id="s1", intent="find the governing invoice-processing procedure", args={"query": "invoice processing three-way match SOP"}),
        step("drive_list", id="s2", intent="see what documents are waiting in the inbox", args={"folder": "invoices-inbox"}),
        step("drive_read", id="s3", intent="read the Helios invoice and extract its details", args={"path": "invoices-inbox/INV-HL-2207.pdf"}),
        step("vendor_lookup", id="s4", intent="confirm Helios Logistics is an approved vendor", args={"name": "Helios Logistics"}),
        step("erp_login", id="s5", intent="sign in to Nexus ERP as the AP clerk", args={"account": "ap_clerk"}),
        step("browser_open", id="s6", intent="check the remaining balance on PO-4482", args={"path": "/purchase-orders/PO-4482"}),
        step("gl_code_lookup", id="s7", intent="get the GL code for the logistics category", args={"category": "logistics"}),
        step("browser_open", id="s8", intent="open the invoice registration form", args={"path": "/invoices/new"}),
        step(
            "browser_fill",
            id="s9",
            intent="enter the Helios invoice details",
            args={
                "fields": {
                    "Invoice number": "INV-HL-2207",
                    "Vendor name": "Helios Logistics",
                    "Purchase order number": "PO-4482",
                    "Invoice amount": "42000",
                    "Invoice date": "2026-09-30",
                    "GL code": GL_LOGISTICS,
                }
            },
        ),
        step("browser_click", id="s10", intent="register the invoice in Nexus ERP", args={"target": "Register invoice"}),
        step("browser_click", id="s11", intent="approve the invoice within clerk authority", args={"target": "Approve invoice"}),
        step(
            "ledger_record",
            id="s12",
            intent="post the approved invoice to the general ledger",
            args={
                "invoice_number": "INV-HL-2207",
                "vendor": "Helios Logistics",
                "po_number": "PO-4482",
                "amount": 42000,
                "currency": "INR",
                "gl_code": GL_LOGISTICS,
                "memo": "Matched to PO-4482 in full; approved within clerk authority.",
            },
        ),
        step(
            "drive_move",
            id="s13",
            intent="file the processed invoice",
            args={"path": "invoices-inbox/INV-HL-2207.pdf", "to_folder": "invoices-processed"},
        ),
        step(
            "record_result",
            id="s14",
            intent="record the outcome for INV-HL-2207",
            args={
                "item": "INV-HL-2207",
                "status": "completed",
                "detail": "Matched PO-4482 (INR 42,000 of 42,000 remaining), approved and posted to the GL.",
                "evidence": ["PO-4482", "ledger"],
            },
        ),
    ]


def _over_billed_invoice_steps() -> list[dict]:
    """Brightpath: INR 186,000 against INR 120,000 remaining -- must not be approved."""
    return [
        step("drive_read", id="s15", intent="read the Brightpath invoice and extract its details", args={"path": "invoices-inbox/INV-BP-0519.pdf"}),
        step("browser_open", id="s16", intent="check the remaining balance on PO-4490", args={"path": "/purchase-orders/PO-4490"}),
        step("browser_open", id="s17", intent="open the invoice registration form", args={"path": "/invoices/new"}),
        step(
            "browser_fill",
            id="s18",
            intent="enter the Brightpath invoice details",
            args={
                "fields": {
                    "Invoice number": "INV-BP-0519",
                    "Vendor name": "Brightpath Consulting LLP",
                    "Purchase order number": "PO-4490",
                    "Invoice amount": "186000",
                    "Invoice date": "2026-09-29",
                }
            },
        ),
        step("browser_click", id="s19", intent="register the invoice so the variance is on record", args={"target": "Register invoice"}),
        step(
            "browser_fill",
            id="s20",
            intent="state the variance as the hold reason",
            args={
                "fields": {
                    "Reason for placing on hold": (
                        "Invoice INR 186,000 exceeds the remaining balance of INR 120,000 on "
                        "PO-4490 (ordered 350,000, already billed 230,000). Excess INR 66,000. "
                        "Referred to the Finance Controller under SOP FIN-007."
                    )
                }
            },
        ),
        step("browser_click", id="s21", intent="place the invoice on hold rather than approving it", args={"target": "Place on hold"}),
        step(
            "drive_move",
            id="s22",
            intent="file the invoice as an exception",
            args={"path": "invoices-inbox/INV-BP-0519.pdf", "to_folder": "invoices-exceptions"},
        ),
        step(
            "record_result",
            id="s23",
            intent="record the outcome for INV-BP-0519",
            args={
                "item": "INV-BP-0519",
                "status": "escalated",
                "detail": "Over-billed by INR 66,000 against PO-4490. Registered, held, not approved.",
                "evidence": ["PO-4490"],
            },
        ),
    ]


VERIFICATION_PROBES = probes(
    {"id": "p1", "criterion_id": "ac1", "tool": "browser_open", "args": {"path": "/invoices"},
     "looking_for": "the status of every invoice in the register"},
    {"id": "p2", "criterion_id": "ac2", "tool": "browser_open", "args": {"path": "/purchase-orders/PO-4490"},
     "looking_for": "PO-4490 remaining balance unchanged at INR 120,000"},
    {"id": "p3", "criterion_id": "ac3", "tool": "ledger_list", "args": {},
     "looking_for": "a ledger entry for every approved invoice"},
    {"id": "p4", "criterion_id": "ac1", "tool": "drive_list", "args": {"folder": "invoices-inbox"},
     "looking_for": "which documents remain unprocessed"},
)


@pytest.fixture
async def operator(tmp_path):
    """Real registry and policy against the real sandbox; local state in tmp."""
    httpx.post(f"{SETTINGS.erp_url}/_sandbox/reset", timeout=15.0)

    settings = dataclasses.replace(
        SETTINGS,
        var_dir=tmp_path / "var",
        artifacts_dir=tmp_path / "artifacts",
        llm_cache=False,
        headless=True,
        budget=Budget(max_steps=60, max_llm_calls=40, wall_clock_seconds=600),
    )
    settings.ensure_dirs()

    memory = MemoryStore(settings.memory_db)
    memory.ingest_company_dir(settings.company_dir)
    events = EventLog(settings.event_db)

    def build(responses):
        provider = ScriptedProvider(list(responses))
        kernel = Kernel(
            settings=settings,
            llm=provider,
            registry=build_registry(settings),
            memory=memory,
            policy=PolicyGuard.from_file(settings.company_dir / "policies.yaml"),
            events=events,
        )
        return kernel

    yield build, settings, events

    memory.close()
    events.close()


def _world() -> dict:
    return httpx.get(f"{SETTINGS.erp_url}/_sandbox/state", timeout=15.0).json()


def _invoice(world: dict, number: str) -> dict | None:
    return next((i for i in world["invoices"] if i["number"] == number), None)


def _po(world: dict, number: str) -> dict:
    return next(po for po in world["purchase_orders"] if po["number"] == number)


class TestFullInvoiceCycle:
    async def test_the_world_actually_changes_as_reported(self, operator):
        build, settings, events = operator
        kernel = build(
            [
                intake("Process the vendor invoices in the inbox", CRITERIA),
                plan(*_clean_invoice_steps(), *_over_billed_invoice_steps()),
                VERIFICATION_PROBES,
                verdict(("ac1", "met"), ("ac2", "met"), ("ac3", "met"), complete=True),
                report("Approved and posted 1 invoice; held 1 for over-billing."),
            ]
        )
        state = await kernel.start(
            "The vendor invoices in the shared drive need processing - please take care of them."
        )

        assert state.status is RunStatus.COMPLETED, state.error
        failed = [s.id for s in state.plan.steps if s.status is StepStatus.FAILED]
        assert not failed, f"steps failed: {failed}"

        world = _world()

        # --- the clean invoice was genuinely approved and posted ---------------
        helios = _invoice(world, "INV-HL-2207")
        assert helios is not None, "the invoice was never registered in the ERP"
        assert helios["status"] == "approved"
        assert helios["approved_by"] == "a.rao"
        assert helios["gl_code"] == GL_LOGISTICS

        assert _po(world, "PO-4482")["billed"] == 42000, "the PO balance must have moved"

        ledger = {entry["invoice_number"]: entry for entry in world["ledger"]}
        assert "INV-HL-2207" in ledger
        assert ledger["INV-HL-2207"]["amount"] == 42000
        assert ledger["INV-HL-2207"]["gl_code"] == GL_LOGISTICS

        # --- the over-billed invoice was NOT approved -------------------------
        brightpath = _invoice(world, "INV-BP-0519")
        assert brightpath is not None
        assert brightpath["status"] == "on_hold", "an over-billed invoice must never be approved"
        assert brightpath["approved_by"] == ""
        assert "66,000" in brightpath["notes"], "the hold reason must carry the figures"

        assert _po(world, "PO-4490")["billed"] == 230000, "the PO must be untouched by a held invoice"
        assert "INV-BP-0519" not in ledger, "a held invoice must not reach the ledger"

        # --- documents were filed ---------------------------------------------
        processed = {p.name for p in (settings.drive_dir / "invoices-processed").glob("*")}
        exceptions = {p.name for p in (settings.drive_dir / "invoices-exceptions").glob("*")}
        assert "INV-HL-2207.pdf" in processed
        assert "INV-BP-0519.pdf" in exceptions

        # --- the evidence bundle is real --------------------------------------
        bundle = settings.artifacts_dir / state.run_id
        report_text = (bundle / "REPORT.md").read_text(encoding="utf-8")
        assert "INV-HL-2207" in report_text
        assert "INV-BP-0519" in report_text
        assert "MET" in report_text
        screenshots = list(bundle.glob("shot-*.png"))
        assert len(screenshots) >= 8, f"expected screenshots of each browser action, got {len(screenshots)}"
        assert all(shot.stat().st_size > 1000 for shot in screenshots), "screenshots must not be blank"

        # --- verification actually re-read the systems ------------------------
        evidence = (bundle / "verification.md").read_text(encoding="utf-8")
        assert "INR 120,000" in evidence, "the verifier must have re-read the PO balance"
        assert "INV-HL-2207" in evidence
        assert all(c.status is CriterionStatus.MET for c in state.verdict.criteria)

    async def test_a_run_that_skips_the_ledger_is_caught_by_verification(self, operator):
        """The failure mode that matters: plausible work, unfinished in fact.

        The plan approves the invoice but never posts it to the ledger. The
        operator's own per-item record still says 'completed'. Verification reads
        the ledger and must contradict it.
        """
        build, settings, events = operator
        steps = [s for s in _clean_invoice_steps() if s.get("id") != "s12"]  # drop ledger_record
        kernel = build(
            [
                intake("Process the vendor invoices in the inbox", CRITERIA),
                plan(*steps),
                VERIFICATION_PROBES,
                # A faithful verifier, reading an empty ledger, must mark ac3 unmet.
                verdict(
                    ("ac1", "met"),
                    ("ac2", "met"),
                    ("ac3", "unmet"),
                    complete=False,
                    remediation=["Post INV-HL-2207 to the general ledger"],
                ),
                plan(step("ledger_record", id="r1", args={
                    "invoice_number": "INV-HL-2207",
                    "vendor": "Helios Logistics",
                    "po_number": "PO-4482",
                    "amount": 42000,
                    "gl_code": GL_LOGISTICS,
                })),
                VERIFICATION_PROBES,
                verdict(("ac1", "met"), ("ac2", "met"), ("ac3", "met"), complete=True),
                report("Posted the missing ledger entry after verification caught it."),
            ]
        )
        state = await kernel.start("process the invoices")

        # Verification drove a remediation round that actually fixed the gap.
        ledger = {entry["invoice_number"] for entry in _world()["ledger"]}
        assert "INV-HL-2207" in ledger, "remediation should have posted the missing entry"
        assert state.status is RunStatus.COMPLETED
        assert state.plan.version == 2, "a second plan should have been produced"

    async def test_an_unapproved_vendor_is_denied_before_any_ledger_write(self, operator):
        """Policy stops the prohibited action; it is not left to the model."""
        build, settings, events = operator
        kernel = build(
            [
                intake("Process the Zenith invoice", [("ac1", "The Zenith invoice is resolved")]),
                plan(
                    step("drive_read", id="s1", args={"path": "invoices-inbox/INV-ZN-7730.pdf"}),
                    step("vendor_lookup", id="s2", args={"name": "Zenith Office Supplies"}),
                    step("ledger_record", id="s3", args={
                        "invoice_number": "INV-ZN-7730",
                        "vendor": "Zenith Office Supplies",
                        "amount": 23400,
                        "gl_code": "6420-OFFICE-SUPPLIES",
                    }),
                ),
                # vendor_lookup fails with not_found -> adapt. Skip, then the
                # denied ledger write also adapts.
                json.dumps({"analysis": "vendor absent from master", "action": "skip",
                            "reason": "FIN-004 blocks payment to unapproved vendors"}),
                json.dumps({"analysis": "policy denied the write", "action": "skip",
                            "reason": "cannot post for an unapproved vendor"}),
                probes({"tool": "ledger_list", "criterion_id": "ac1", "args": {}}),
                verdict(("ac1", "unmet"), complete=False),
                report("The Zenith invoice cannot be processed: vendor not approved."),
            ]
        )
        state = await kernel.start("process the Zenith invoice")

        ledger = {entry["invoice_number"] for entry in _world()["ledger"]}
        assert "INV-ZN-7730" not in ledger, "a denied ledger write must never happen"

        types = [event.type for event in events.events(state.run_id)]
        assert "policy.denied" in types
        assert state.status is not RunStatus.COMPLETED, "it must not claim success"

    async def test_a_server_error_on_submit_is_not_mistaken_for_success(self, operator):
        """A 503 on the real registration POST must surface, then be recovered.

        The dangerous outcome here is silence: the click resolves, the page
        changes, and a naive tool reports success while nothing was saved. The
        operator must notice, re-establish the form, and submit again -- and must
        end up with exactly one invoice, not two.
        """
        build, settings, events = operator
        httpx.post(
            f"{SETTINGS.erp_url}/_sandbox/chaos",
            json={"key": "erp.invoice.create", "fault": "503", "times": 1},
            timeout=10.0,
        )
        refill = _clean_invoice_steps()[7:9]  # browser_open /invoices/new, browser_fill
        kernel = build(
            [
                intake("Register the Helios invoice", [("ac1", "INV-HL-2207 is registered and approved")]),
                plan(*_clean_invoice_steps()[:11]),
                # browser_click is non-idempotent, so the failure reaches adapt
                # rather than being blindly re-fired.
                json.dumps(
                    {
                        "analysis": "The POST returned 503, so the invoice was never created.",
                        "action": "insert_steps",
                        "reason": "Re-open the form and re-enter the values, then submit again.",
                        "steps": [
                            {
                                "id": "fix1",
                                "intent": "re-open the registration form",
                                "tool": "browser_open",
                                "args_json": json.dumps(refill[0]["args"]),
                            },
                            {
                                "id": "fix2",
                                "intent": "re-enter the invoice details",
                                "tool": "browser_fill",
                                "args_json": json.dumps(refill[1]["args"]),
                            },
                        ],
                    }
                ),
                probes({"tool": "browser_open", "criterion_id": "ac1", "args": {"path": "/invoices"}}),
                verdict(("ac1", "met"), complete=True),
                report("Registered after recovering from a transient ERP fault."),
            ]
        )
        state = await kernel.start("register the Helios invoice")

        world = _world()
        matching = [i for i in world["invoices"] if i["number"] == "INV-HL-2207"]
        assert len(matching) == 1, "recovery must not create a duplicate invoice"
        assert matching[0]["status"] == "approved"

        types = [event.type for event in events.events(state.run_id)]
        assert "adapt.decided" in types, "a non-idempotent failure must go to adapt, not a blind retry"
        assert "plan.steps_inserted" in types
        assert state.status is RunStatus.COMPLETED

    async def test_a_transient_fault_on_an_idempotent_tool_costs_no_reasoning(self, operator):
        """The ledger write is declared idempotent, so a 503 is just retried."""
        build, settings, events = operator
        httpx.post(
            f"{SETTINGS.erp_url}/_sandbox/chaos",
            json={"key": "api.ledger.create", "fault": "503", "times": 1},
            timeout=10.0,
        )
        kernel = build(
            [
                intake("Post the entry", [("ac1", "The entry is in the ledger")]),
                plan(
                    step("ledger_record", id="s1", args={
                        "invoice_number": "INV-HL-2207",
                        "vendor": "Helios Logistics",
                        "amount": 42000,
                        "gl_code": GL_LOGISTICS,
                    })
                ),
                probes({"tool": "ledger_list", "criterion_id": "ac1", "args": {}}),
                verdict(("ac1", "met"), complete=True),
                report("Posted after one retry."),
            ]
        )
        state = await kernel.start("post the ledger entry")

        ledger = [e for e in _world()["ledger"] if e["invoice_number"] == "INV-HL-2207"]
        assert len(ledger) == 1, "the idempotent endpoint must not double-post"

        types = [event.type for event in events.events(state.run_id)]
        assert "step.retrying" in types
        assert "adapt.decided" not in types, "a retryable idempotent fault needs no model call"
        assert state.status is RunStatus.COMPLETED
