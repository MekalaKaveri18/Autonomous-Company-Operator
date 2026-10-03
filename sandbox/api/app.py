"""FinanceOps API -- a mock internal JSON service.

A second, structurally different system on purpose. If the operator only ever
drove a browser, the connector abstraction would be untested: one surface is not
an abstraction. Here the vendor master and the general ledger are reached over
HTTP with JSON, while approvals happen in a server-rendered UI, and the kernel
treats both identically because both are just tools.

The ledger write is idempotent on invoice number, which is what lets the
operator retry a failed POST safely -- and what lets the verifier re-run its
checks without creating duplicate entries.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sandbox.store import STORE  # noqa: E402

app = FastAPI(title="FinanceOps API (sandbox)", docs_url="/docs")


def _fault_response(key: str) -> JSONResponse | None:
    fault = STORE.consume_fault(key)
    if fault is None:
        return None
    if fault == "503":
        return JSONResponse(
            {"error": "service_unavailable", "detail": "FinanceOps API is briefly unavailable."},
            status_code=503,
        )
    if fault == "500":
        return JSONResponse({"error": "internal", "detail": "Unhandled error."}, status_code=500)
    if fault == "timeout":
        time.sleep(30)
    return None


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "financeops-api"}


@app.get("/vendors")
async def lookup_vendor(name: str = "", response: Response = None) -> JSONResponse:  # type: ignore[assignment]
    """Vendor master lookup.

    A vendor that is absent is a **404 with a useful body**, not an empty 200.
    The distinction matters: the operator must be able to tell "this vendor is
    not approved" from "the lookup returned nothing useful".
    """
    if not name.strip():
        return JSONResponse(
            {"error": "missing_parameter", "detail": "Query parameter 'name' is required."},
            status_code=400,
        )
    vendor = STORE.vendor_by_name(name)
    if vendor is None:
        return JSONResponse(
            {
                "error": "vendor_not_found",
                "detail": (
                    f"No vendor matching '{name}' exists in the approved vendor master. "
                    "Payments to unapproved vendors are blocked by policy FIN-004."
                ),
                "searched_for": name,
                "approved_vendor_count": len(STORE.vendors()),
            },
            status_code=404,
        )
    return JSONResponse({"vendor": vendor})


@app.get("/vendors/all")
async def list_vendors() -> dict[str, Any]:
    return {"vendors": STORE.vendors()}


@app.get("/gl-codes")
async def gl_codes(category: str = "") -> JSONResponse:
    codes: dict[str, str] = STORE.load().get("gl_codes", {})
    if not category:
        return JSONResponse({"gl_codes": codes})
    code = codes.get(category.strip().lower())
    if code is None:
        return JSONResponse(
            {
                "error": "unknown_category",
                "detail": f"No GL code mapped for category '{category}'.",
                "known_categories": sorted(codes),
            },
            status_code=404,
        )
    return JSONResponse({"category": category, "gl_code": code})


@app.get("/ledger/entries")
async def list_ledger() -> dict[str, Any]:
    return {"entries": STORE.load().get("ledger", [])}


@app.post("/ledger/entries")
async def create_ledger_entry(payload: dict) -> JSONResponse:
    """Record an approved invoice in the GL. Idempotent on ``invoice_number``."""
    fault = _fault_response("api.ledger.create")
    if fault is not None:
        return fault

    required = ["invoice_number", "vendor", "amount", "gl_code"]
    missing = [field for field in required if not str(payload.get(field, "")).strip()]
    if missing:
        return JSONResponse(
            {
                "error": "missing_fields",
                "detail": f"Required field(s) absent: {', '.join(missing)}.",
                "required": required,
            },
            status_code=422,
        )

    try:
        amount = float(str(payload["amount"]).replace(",", ""))
    except ValueError:
        return JSONResponse(
            {"error": "invalid_amount", "detail": f"amount '{payload['amount']}' is not numeric."},
            status_code=422,
        )

    invoice_number = str(payload["invoice_number"]).strip()

    def apply(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        ledger: list[dict[str, Any]] = state.setdefault("ledger", [])
        existing = next(
            (e for e in ledger if e["invoice_number"].lower() == invoice_number.lower()), None
        )
        if existing is not None:
            return existing, False
        entry = {
            "entry_id": f"GL-{len(ledger) + 1:05d}",
            "invoice_number": invoice_number,
            "vendor": str(payload["vendor"]).strip(),
            "po_number": str(payload.get("po_number", "")).strip(),
            "amount": amount,
            "currency": str(payload.get("currency", "INR")),
            "gl_code": str(payload["gl_code"]).strip(),
            "memo": str(payload.get("memo", "")).strip(),
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        ledger.append(entry)
        state.setdefault("audit", []).append(
            {"action": "ledger.recorded", "invoice": invoice_number, "entry": entry["entry_id"]}
        )
        return entry, True

    entry, created = STORE.mutate(apply)
    return JSONResponse(
        {"entry": entry, "created": created, "idempotent_replay": not created},
        status_code=201 if created else 200,
    )


@app.get("/audit")
async def audit_trail() -> dict[str, Any]:
    return {"audit": STORE.load().get("audit", [])}


# ---------------------------------------------------------------------------
# IT operations: READ ONLY.
#
# Deliberately asymmetric. Accounts, assets and access groups can be read here
# but changed only through the Asset and Access Manager desktop application.
# That asymmetry is extremely common in real estates -- a read API gets bolted on
# for reporting years after the GUI -- and it is what forces the operator to
# actually drive a desktop app rather than taking the easy HTTP route.
# ---------------------------------------------------------------------------


@app.get("/itops/accounts")
async def list_accounts(username: str = "") -> JSONResponse:
    accounts = STORE.load().get("accounts", [])
    if not username:
        return JSONResponse({"accounts": accounts, "count": len(accounts)})
    match = next(
        (a for a in accounts if a["username"].lower() == username.strip().lower()), None
    )
    if match is None:
        return JSONResponse(
            {
                "error": "account_not_found",
                "detail": f"No account '{username}' exists in the directory.",
                "searched_for": username,
            },
            status_code=404,
        )
    return JSONResponse({"account": match})


@app.get("/itops/assets")
async def list_assets(assigned_to: str = "", status: str = "") -> JSONResponse:
    assets = STORE.load().get("assets", [])
    if assigned_to:
        assets = [a for a in assets if a["assigned_to"].lower() == assigned_to.strip().lower()]
    if status:
        assets = [a for a in assets if a["status"] == status.strip().lower()]
    return JSONResponse({"assets": assets, "count": len(assets)})


@app.get("/itops/access-groups")
async def list_access_groups(member: str = "") -> JSONResponse:
    groups = STORE.load().get("access_groups", {})
    if member:
        needle = member.strip().lower()
        groups = {
            name: info
            for name, info in groups.items()
            if needle in {m.lower() for m in info.get("members", [])}
        }
    return JSONResponse({"access_groups": groups, "count": len(groups)})
