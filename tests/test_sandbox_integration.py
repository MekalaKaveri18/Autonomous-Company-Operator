"""End-to-end tool tests against the live sandbox.

These exercise the parts a scripted model cannot reach: real Chromium driving the
real ERP, real HTTP against the connector-defined API, real PDF extraction from
the drive. They are the evidence that the operator's hands work, separately from
the evidence that its head works.

Skipped automatically when the sandbox is not running, so the default suite stays
runnable with nothing started.
"""

from __future__ import annotations

import httpx
import pytest

from centralign.config import SETTINGS
from centralign.memory.store import MemoryStore
from centralign.events.log import EventLog
from centralign.runtime.state import RiskTier, RunState
from centralign.tools.base import FailureKind, ToolContext
from centralign.tools.browser_tools import BROWSER_TOOLS
from centralign.tools.connector import load_connectors
from centralign.tools.drive_tools import DRIVE_TOOLS
from centralign.tools.registry import ToolRegistry


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


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry([*DRIVE_TOOLS, *BROWSER_TOOLS])
    for tool in load_connectors(SETTINGS.company_dir / "connectors"):
        reg.register(tool)
    return reg


@pytest.fixture
async def ctx(tmp_path):
    """A tool context against the real sandbox, with throwaway local state.

    Async so that Chromium and the HTTP client are torn down on the same event
    loop that created them -- closing them from a fresh loop leaves dangling
    subprocess transports on Windows.
    """
    SETTINGS.ensure_dirs()
    httpx.post(f"{SETTINGS.erp_url}/_sandbox/reset", timeout=10.0)

    events = EventLog(tmp_path / "events.sqlite3")
    memory = MemoryStore(tmp_path / "memory.sqlite3")
    context = ToolContext(
        settings=SETTINGS,
        run_id="test-run",
        events=events,
        state=RunState(run_id="test-run", goal="integration test"),
        memory=memory,
        artifacts_dir=tmp_path / "artifacts",
    )
    try:
        yield context
    finally:
        session = context.resources.pop("browser", None)
        if session is not None:
            await session.close()
        client = context.resources.pop("http_client", None)
        if client is not None:
            await client.aclose()
        events.close()
        memory.close()


# ---------------------------------------------------------------------------
# drive surface
# ---------------------------------------------------------------------------


class TestDriveSurface:
    async def test_lists_the_inbox(self, registry, ctx):
        observation = await registry.get("drive_list").invoke({"folder": "invoices-inbox"}, ctx)
        assert observation.ok
        names = [f["name"] for f in observation.data["files"]]
        assert "INV-BP-0519.pdf" in names
        assert "canteen-menu-october.pdf" in names, "distractors must be visible, not filtered"

    async def test_extracts_text_from_a_real_pdf(self, registry, ctx):
        observation = await registry.get("drive_read").invoke(
            {"path": "invoices-inbox/INV-BP-0519.pdf"}, ctx
        )
        assert observation.ok
        text = observation.data["text"]
        assert "INV-BP-0519" in text
        assert "PO-4490" in text
        assert "186,000" in text

    async def test_a_missing_file_is_not_found_not_a_crash(self, registry, ctx):
        observation = await registry.get("drive_read").invoke(
            {"path": "invoices-inbox/nope.pdf"}, ctx
        )
        assert not observation.ok
        assert observation.error_kind == FailureKind.NOT_FOUND
        assert not observation.retryable

    async def test_path_traversal_is_refused(self, registry, ctx):
        observation = await registry.get("drive_read").invoke(
            {"path": "../../company/credentials.sandbox.yaml"}, ctx
        )
        assert not observation.ok
        assert observation.error_kind == FailureKind.PERMISSION

    async def test_filing_is_idempotent(self, registry, ctx):
        move = registry.get("drive_move")
        first = await move.invoke(
            {"path": "invoices-inbox/canteen-menu-october.pdf", "to_folder": "invoices-exceptions"},
            ctx,
        )
        assert first.ok and first.data["already_present"] is False

        # Re-filing must report success, so a retried step is not a hard failure.
        second = await move.invoke(
            {"path": "invoices-exceptions/canteen-menu-october.pdf", "to_folder": "invoices-exceptions"},
            ctx,
        )
        assert second.ok


# ---------------------------------------------------------------------------
# declarative connector surface
# ---------------------------------------------------------------------------


class TestConnectorSurface:
    async def test_connector_tools_are_loaded_from_yaml(self, registry):
        assert registry.has("vendor_lookup")
        assert registry.has("ledger_record")
        assert registry.get("ledger_record").risk is RiskTier.SENSITIVE
        assert registry.get("ledger_record").reversible is False

    async def test_approved_vendor_is_found(self, registry, ctx):
        observation = await registry.get("vendor_lookup").invoke({"name": "Helios"}, ctx)
        assert observation.ok
        assert observation.data["response"]["vendor"]["approved"] is True

    async def test_unapproved_vendor_maps_to_not_found(self, registry, ctx):
        observation = await registry.get("vendor_lookup").invoke({"name": "Zenith Office Supplies"}, ctx)
        assert not observation.ok
        assert observation.error_kind == FailureKind.NOT_FOUND
        assert not observation.retryable, "a missing vendor must never be retried"
        assert "FIN-004" in observation.summary

    async def test_gl_code_lookup(self, registry, ctx):
        observation = await registry.get("gl_code_lookup").invoke({"category": "logistics"}, ctx)
        assert observation.ok
        assert observation.data["response"]["gl_code"] == "5240-INBOUND-FREIGHT"

    async def test_ledger_write_is_idempotent(self, registry, ctx):
        args = {
            "invoice_number": "INV-TEST-001",
            "vendor": "Helios Logistics",
            "po_number": "PO-4482",
            "amount": 42000,
            "gl_code": "5240-INBOUND-FREIGHT",
        }
        first = await registry.get("ledger_record").invoke(args, ctx)
        second = await registry.get("ledger_record").invoke(args, ctx)

        assert first.ok and second.ok
        assert first.data["response"]["created"] is True
        assert second.data["response"]["idempotent_replay"] is True
        entry_id = first.data["response"]["entry"]["entry_id"]
        assert second.data["response"]["entry"]["entry_id"] == entry_id

    async def test_missing_required_field_is_classified_as_invalid_args(self, registry, ctx):
        observation = await registry.get("ledger_record").invoke(
            {"invoice_number": "INV-X", "vendor": "Helios Logistics", "amount": 1, "gl_code": ""},
            ctx,
        )
        assert not observation.ok
        assert observation.error_kind == FailureKind.INVALID_ARGS

    async def test_a_transient_503_is_classified_retryable_then_succeeds(self, registry, ctx):
        """Injected fault, so recovery is demonstrable rather than hoped for."""
        httpx.post(
            f"{SETTINGS.erp_url}/_sandbox/chaos",
            json={"key": "api.ledger.create", "fault": "503", "times": 1},
            timeout=10.0,
        )
        args = {
            "invoice_number": "INV-CHAOS-1",
            "vendor": "Helios Logistics",
            "amount": 100,
            "gl_code": "5240-INBOUND-FREIGHT",
        }
        failed = await registry.get("ledger_record").invoke(args, ctx)
        assert not failed.ok
        assert failed.error_kind == FailureKind.UNAVAILABLE
        assert failed.retryable is True

        # The fault was consumed, so the retry the kernel would perform succeeds.
        retried = await registry.get("ledger_record").invoke(args, ctx)
        assert retried.ok


# ---------------------------------------------------------------------------
# browser surface -- real Chromium against the real ERP
# ---------------------------------------------------------------------------


class TestBrowserSurface:
    async def test_login_uses_the_vault_and_never_exposes_the_password(self, registry, ctx):
        observation = await registry.get("erp_login").invoke({"account": "ap_clerk"}, ctx)
        assert observation.ok
        assert "Ananya Rao" in observation.summary
        assert "operator-sandbox" not in observation.summary, "password must never reach the model"
        assert observation.artifacts, "every browser action should leave a screenshot"

    async def test_switching_to_the_controller_account_is_sensitive(self, registry):
        tool = registry.get("erp_login")
        assert tool.risk_for({"account": "ap_clerk"}) is RiskTier.READ
        assert tool.risk_for({"account": "finance_controller"}) is RiskTier.SENSITIVE

    async def test_acting_before_login_is_a_precondition_failure(self, registry, ctx):
        observation = await registry.get("browser_open").invoke({"path": "/invoices"}, ctx)
        assert not observation.ok
        assert observation.error_kind == FailureKind.PRECONDITION
        assert "erp_login" in (observation.error or "")

    async def test_reads_the_remaining_po_balance(self, registry, ctx):
        await registry.get("erp_login").invoke({"account": "ap_clerk"}, ctx)
        observation = await registry.get("browser_open").invoke(
            {"path": "/purchase-orders/PO-4490"}, ctx
        )
        assert observation.ok
        page = observation.data["page"]
        # The partially-billed PO is the whole point of the over-billing case.
        assert "Remaining balance: INR 120,000" in page
        assert "Already billed: INR 230,000" in page

    async def test_a_missing_purchase_order_renders_as_not_found(self, registry, ctx):
        await registry.get("erp_login").invoke({"account": "ap_clerk"}, ctx)
        observation = await registry.get("browser_open").invoke(
            {"path": "/purchase-orders/PO-4503"}, ctx
        )
        # A 404 page is an observation to reason about, not a tool failure.
        assert observation.ok
        assert "not found" in observation.data["page"].lower()

    async def test_registers_an_invoice_through_the_form(self, registry, ctx):
        await registry.get("erp_login").invoke({"account": "ap_clerk"}, ctx)
        await registry.get("browser_open").invoke({"path": "/invoices/new"}, ctx)
        filled = await registry.get("browser_fill").invoke(
            {
                "fields": {
                    "Invoice number": "INV-HL-2207",
                    "Vendor name": "Helios Logistics",
                    "Purchase order number": "PO-4482",
                    "Invoice amount": "42000",
                    "Invoice date": "2026-09-30",
                    "GL code": "5240-INBOUND-FREIGHT",
                }
            },
            ctx,
        )
        assert filled.ok, filled.error
        clicked = await registry.get("browser_click").invoke({"target": "Register invoice"}, ctx)
        assert clicked.ok
        assert "registered successfully" in clicked.data["page"].lower()
        assert clicked.data["refused"] is False

    async def test_a_duplicate_registration_is_refused_with_a_reason(self, registry, ctx):
        await registry.get("erp_login").invoke({"account": "ap_clerk"}, ctx)
        await registry.get("browser_open").invoke({"path": "/invoices/new"}, ctx)
        await registry.get("browser_fill").invoke(
            {
                "fields": {
                    "Invoice number": "INV-ML-1188",
                    "Vendor name": "Meridian Testing Labs",
                    "Purchase order number": "PO-4455",
                    "Invoice amount": "18750",
                }
            },
            ctx,
        )
        clicked = await registry.get("browser_click").invoke({"target": "Register invoice"}, ctx)
        assert clicked.ok, "a refusal is a successful observation of a 'no'"
        assert clicked.data["refused"] is True
        assert "already registered" in clicked.data["page"].lower()

    async def test_the_erp_refuses_an_approval_beyond_clerk_authority(self, registry, ctx):
        """Second line of defence, independent of the operator's own policy."""
        await registry.get("erp_login").invoke({"account": "ap_clerk"}, ctx)
        await registry.get("browser_open").invoke({"path": "/invoices/new"}, ctx)
        await registry.get("browser_fill").invoke(
            {
                "fields": {
                    "Invoice number": "INV-NW-8841",
                    "Vendor name": "Northwind Components Pvt Ltd",
                    "Purchase order number": "PO-4471",
                    "Invoice amount": "128400",
                }
            },
            ctx,
        )
        await registry.get("browser_click").invoke({"target": "Register invoice"}, ctx)
        clicked = await registry.get("browser_click").invoke({"target": "Approve invoice"}, ctx)

        assert clicked.data["refused"] is True
        page = clicked.data["page"]
        assert "exceeds your approval limit" in page
        assert "Finance Controller" in page

    async def test_the_controller_account_can_approve_the_same_invoice(self, registry, ctx):
        await registry.get("erp_login").invoke({"account": "ap_clerk"}, ctx)
        await registry.get("browser_open").invoke({"path": "/invoices/new"}, ctx)
        await registry.get("browser_fill").invoke(
            {
                "fields": {
                    "Invoice number": "INV-NW-8841",
                    "Vendor name": "Northwind Components Pvt Ltd",
                    "Purchase order number": "PO-4471",
                    "Invoice amount": "128400",
                }
            },
            ctx,
        )
        registered = await registry.get("browser_click").invoke({"target": "Register invoice"}, ctx)
        invoice_url = registered.data["url"]

        await registry.get("erp_login").invoke({"account": "finance_controller"}, ctx)
        await registry.get("browser_open").invoke({"path": invoice_url}, ctx)
        clicked = await registry.get("browser_click").invoke({"target": "Approve invoice"}, ctx)

        assert clicked.data["refused"] is False, clicked.data["page"][:500]
        assert "approved" in clicked.data["page"].lower()

    async def test_over_billing_is_refused_with_the_figures(self, registry, ctx):
        await registry.get("erp_login").invoke({"account": "finance_controller"}, ctx)
        await registry.get("browser_open").invoke({"path": "/invoices/new"}, ctx)
        await registry.get("browser_fill").invoke(
            {
                "fields": {
                    "Invoice number": "INV-BP-0519",
                    "Vendor name": "Brightpath Consulting LLP",
                    "Purchase order number": "PO-4490",
                    "Invoice amount": "186000",
                }
            },
            ctx,
        )
        await registry.get("browser_click").invoke({"target": "Register invoice"}, ctx)
        clicked = await registry.get("browser_click").invoke({"target": "Approve invoice"}, ctx)

        assert clicked.data["refused"] is True
        page = clicked.data["page"]
        assert "exceeds the remaining balance" in page
        assert "120,000" in page and "186,000" in page

    async def test_clicking_something_absent_lists_what_is_available(self, registry, ctx):
        await registry.get("erp_login").invoke({"account": "ap_clerk"}, ctx)
        await registry.get("browser_open").invoke({"path": "/dashboard"}, ctx)
        observation = await registry.get("browser_click").invoke({"target": "Launch the rocket"}, ctx)

        assert not observation.ok
        assert observation.error_kind == FailureKind.NOT_FOUND
        # Recovery depends on the failure saying what *is* possible.
        assert "available --" in (observation.error or "")

    async def test_placing_an_invoice_on_hold_records_the_reason(self, registry, ctx):
        await registry.get("erp_login").invoke({"account": "ap_clerk"}, ctx)
        await registry.get("browser_open").invoke({"path": "/invoices/INVC-0001"}, ctx)
        await registry.get("browser_fill").invoke(
            {"fields": {"Reason for placing on hold": "Variance of INR 66,000 against PO-4490."}}, ctx
        )
        clicked = await registry.get("browser_click").invoke({"target": "Place on hold"}, ctx)
        assert clicked.ok
        assert "on hold" in clicked.data["page"].lower()
        assert "66,000" in clicked.data["page"]
