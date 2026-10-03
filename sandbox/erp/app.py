"""Nexus ERP -- a mock enterprise web app, and the operator's browser target.

Deliberately built like an ordinary internal tool rather than something
agent-friendly: server-rendered forms, a cookie session, validation errors shown
as page content rather than structured JSON, and no ``data-testid`` hooks. The
operator has to log in, navigate, read tables and interpret an error banner, the
way a person would.

Two independent safety layers matter here, and they are not redundant:

1. The operator's own :class:`~centralign.policy.guard.PolicyGuard` stops an
   over-authority approval *before* it is attempted.
2. This app refuses it anyway, because an internal system cannot trust its
   caller. If the agent's reasoning fails, the ERP still says no -- and the
   resulting error is something the agent must then recover from.

The 3-way match (invoice vs PO vs remaining balance) is intentionally **not**
performed at registration. Noticing that an invoice over-bills its PO is the
operator's job; the approval endpoint is only a backstop.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sandbox.store import STORE  # noqa: E402

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["inr"] = lambda value: f"{int(value):,}"

app = FastAPI(title="Nexus ERP (sandbox)", docs_url=None, redoc_url=None)

SESSION_COOKIE = "nexus_session"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def current_user(request: Request) -> dict[str, Any] | None:
    username = request.cookies.get(SESSION_COOKIE)
    return STORE.user(username) if username else None


def render(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request=request,
        name=template,
        context={"user": current_user(request), **context},
        status_code=status_code,
    )


def login_redirect() -> RedirectResponse:
    return RedirectResponse("/?error=Please+sign+in+to+continue", status_code=303)


def po_remaining(po: dict[str, Any]) -> int:
    return int(po["total"]) - int(po.get("billed", 0))


def _fault(key: str) -> JSONResponse | None:
    """Honour an injected fault, if one is queued for this key."""
    fault = STORE.consume_fault(key)
    if fault is None:
        return None
    if fault == "503":
        return JSONResponse(
            {"detail": "Nexus ERP is temporarily unavailable. Please retry."}, status_code=503
        )
    if fault == "500":
        return JSONResponse({"detail": "Internal server error."}, status_code=500)
    return None


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "") -> HTMLResponse:
    if current_user(request):
        return RedirectResponse("/dashboard", status_code=303)  # type: ignore[return-value]
    return render(request, "login.html", {"error": error})


@app.post("/login")
async def login(username: str = Form(...), password: str = Form(...)) -> RedirectResponse:
    user = STORE.user(username)
    if not user or user["password"] != password:
        return RedirectResponse("/?error=Invalid+username+or+password", status_code=303)
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(SESSION_COOKIE, user["username"], httponly=False, samesite="lax")
    STORE.audit("login", {"user": user["username"]})
    return response


@app.get("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ---------------------------------------------------------------------------
# read-only screens
# ---------------------------------------------------------------------------


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    if not current_user(request):
        return login_redirect()  # type: ignore[return-value]
    state = STORE.load()
    invoices = state["invoices"]
    return render(
        request,
        "dashboard.html",
        {
            "counts": {
                "open_pos": sum(1 for po in state["purchase_orders"] if po["status"] == "open"),
                "registered": sum(1 for i in invoices if i["status"] == "registered"),
                "approved": sum(1 for i in invoices if i["status"] == "approved"),
                "on_hold": sum(1 for i in invoices if i["status"] == "on_hold"),
            }
        },
    )


@app.get("/purchase-orders", response_class=HTMLResponse)
async def purchase_orders(request: Request, q: str = "") -> HTMLResponse:
    if not current_user(request):
        return login_redirect()  # type: ignore[return-value]
    needle = q.strip().lower()
    rows = [
        {**po, "remaining": po_remaining(po)}
        for po in STORE.purchase_orders()
        if not needle
        or needle in po["number"].lower()
        or needle in po["vendor"].lower()
    ]
    return render(request, "purchase_orders.html", {"rows": rows, "q": q})


@app.get("/purchase-orders/{number}", response_class=HTMLResponse)
async def purchase_order_detail(request: Request, number: str) -> HTMLResponse:
    if not current_user(request):
        return login_redirect()  # type: ignore[return-value]
    po = STORE.po_by_number(number)
    if not po:
        return render(
            request,
            "not_found.html",
            {
                "what": "Purchase order",
                "identifier": number,
                "hint": "No purchase order with this number exists in the PO master.",
            },
            status_code=404,
        )
    linked = [i for i in STORE.invoices() if i["po_number"].lower() == po["number"].lower()]
    return render(
        request,
        "purchase_order_detail.html",
        {"po": po, "remaining": po_remaining(po), "invoices": linked},
    )


@app.get("/vendors", response_class=HTMLResponse)
async def vendors(request: Request, q: str = "") -> HTMLResponse:
    if not current_user(request):
        return login_redirect()  # type: ignore[return-value]
    needle = q.strip().lower()
    rows = [v for v in STORE.vendors() if not needle or needle in v["name"].lower()]
    return render(request, "vendors.html", {"rows": rows, "q": q})


@app.get("/invoices", response_class=HTMLResponse)
async def invoices(request: Request, q: str = "") -> HTMLResponse:
    if not current_user(request):
        return login_redirect()  # type: ignore[return-value]
    needle = q.strip().lower()
    rows = [
        i
        for i in STORE.invoices()
        if not needle or needle in i["number"].lower() or needle in i["vendor"].lower()
    ]
    return render(request, "invoices.html", {"rows": rows, "q": q})


@app.get("/invoices/new", response_class=HTMLResponse)
async def new_invoice_form(request: Request, error: str = "") -> HTMLResponse:
    if not current_user(request):
        return login_redirect()  # type: ignore[return-value]
    return render(request, "invoice_new.html", {"error": error})


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
async def invoice_detail(request: Request, invoice_id: str, error: str = "", notice: str = "") -> HTMLResponse:
    if not current_user(request):
        return login_redirect()  # type: ignore[return-value]
    invoice = STORE.invoice_by_id(invoice_id) or STORE.invoice_by_number(invoice_id)
    if not invoice:
        return render(
            request,
            "not_found.html",
            {"what": "Invoice", "identifier": invoice_id, "hint": "No such invoice record."},
            status_code=404,
        )
    po = STORE.po_by_number(invoice["po_number"])
    return render(
        request,
        "invoice_detail.html",
        {
            "invoice": invoice,
            "po": po,
            "remaining": po_remaining(po) if po else None,
            "error": error,
            "notice": notice,
        },
    )


# ---------------------------------------------------------------------------
# mutating screens
# ---------------------------------------------------------------------------


@app.post("/invoices")
async def create_invoice(
    request: Request,
    number: str = Form(...),
    vendor: str = Form(...),
    po_number: str = Form(...),
    amount: str = Form(...),
    invoice_date: str = Form(""),
    gl_code: str = Form(""),
):
    user = current_user(request)
    if not user:
        return login_redirect()

    fault = _fault("erp.invoice.create")
    if fault is not None:
        return fault

    def fail(message: str) -> RedirectResponse:
        from urllib.parse import quote

        return RedirectResponse(f"/invoices/new?error={quote(message)}", status_code=303)

    try:
        amount_value = int(float(str(amount).replace(",", "").strip()))
    except ValueError:
        return fail(f"Amount '{amount}' is not a valid number.")

    if STORE.invoice_by_number(number):
        return fail(
            f"Invoice {number} is already registered in Nexus ERP. "
            "Duplicate registration is blocked."
        )

    po = STORE.po_by_number(po_number)
    if not po:
        return fail(
            f"Purchase order {po_number} was not found in the PO master. "
            "An invoice cannot be registered without a valid purchase order."
        )

    # Note: no 3-way match here on purpose. Registration records what the
    # vendor claims; reconciling it against the PO is the operator's job.
    def apply(state: dict[str, Any]) -> dict[str, Any]:
        seq = state.get("next_invoice_seq", 1)
        state["next_invoice_seq"] = seq + 1
        record = {
            "id": f"INVC-{seq:04d}",
            "number": number.strip(),
            "vendor": vendor.strip(),
            "po_number": po["number"],
            "amount": amount_value,
            "currency": po.get("currency", "INR"),
            "invoice_date": invoice_date.strip(),
            "status": "registered",
            "gl_code": gl_code.strip(),
            "registered_by": user["username"],
            "approved_by": "",
            "notes": "",
            "history": [{"action": "registered", "by": user["username"]}],
        }
        state["invoices"].append(record)
        state.setdefault("audit", []).append(
            {"action": "invoice.registered", "invoice": record["number"], "by": user["username"]}
        )
        return record

    record = STORE.mutate(apply)
    return RedirectResponse(
        f"/invoices/{record['id']}?notice=Invoice+registered+successfully", status_code=303
    )


@app.post("/invoices/{invoice_id}/approve")
async def approve_invoice(request: Request, invoice_id: str):
    user = current_user(request)
    if not user:
        return login_redirect()

    fault = _fault("erp.invoice.approve")
    if fault is not None:
        return fault

    invoice = STORE.invoice_by_id(invoice_id) or STORE.invoice_by_number(invoice_id)
    if not invoice:
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

    from urllib.parse import quote

    def refuse(message: str) -> RedirectResponse:
        STORE.audit(
            "invoice.approval_refused",
            {"invoice": invoice["number"], "by": user["username"], "reason": message},
        )
        return RedirectResponse(
            f"/invoices/{invoice['id']}?error={quote(message)}", status_code=303
        )

    if invoice["status"] == "approved":
        return refuse(f"Invoice {invoice['number']} is already approved. No action taken.")

    # Layer 2: authority check. The operator's policy should have caught this,
    # but an internal system must not depend on its caller being correct.
    if int(invoice["amount"]) > int(user["approval_limit"]):
        return refuse(
            f"Amount {invoice['currency']} {int(invoice['amount']):,} exceeds your approval "
            f"limit of {invoice['currency']} {int(user['approval_limit']):,} "
            f"({user['role']}). Approval must be performed by a Finance Controller."
        )

    po = STORE.po_by_number(invoice["po_number"])
    if not po:
        return refuse(f"Purchase order {invoice['po_number']} no longer exists.")

    remaining = po_remaining(po)
    if int(invoice["amount"]) > remaining:
        return refuse(
            f"Amount {invoice['currency']} {int(invoice['amount']):,} exceeds the remaining "
            f"balance of {invoice['currency']} {remaining:,} on {po['number']} "
            f"(ordered {int(po['total']):,}, already billed {int(po.get('billed', 0)):,})."
        )

    def apply(state: dict[str, Any]) -> None:
        for record in state["invoices"]:
            if record["id"] == invoice["id"]:
                record["status"] = "approved"
                record["approved_by"] = user["username"]
                record.setdefault("history", []).append(
                    {"action": "approved", "by": user["username"]}
                )
        for order in state["purchase_orders"]:
            if order["number"] == po["number"]:
                order["billed"] = int(order.get("billed", 0)) + int(invoice["amount"])
        state.setdefault("audit", []).append(
            {"action": "invoice.approved", "invoice": invoice["number"], "by": user["username"]}
        )

    STORE.mutate(apply)
    return RedirectResponse(
        f"/invoices/{invoice['id']}?notice=Invoice+approved", status_code=303
    )


@app.post("/invoices/{invoice_id}/hold")
async def hold_invoice(request: Request, invoice_id: str, reason: str = Form(...)):
    user = current_user(request)
    if not user:
        return login_redirect()

    invoice = STORE.invoice_by_id(invoice_id) or STORE.invoice_by_number(invoice_id)
    if not invoice:
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

    def apply(state: dict[str, Any]) -> None:
        for record in state["invoices"]:
            if record["id"] == invoice["id"]:
                record["status"] = "on_hold"
                record["notes"] = reason.strip()
                record.setdefault("history", []).append(
                    {"action": "held", "by": user["username"], "reason": reason.strip()}
                )
        state.setdefault("audit", []).append(
            {
                "action": "invoice.held",
                "invoice": invoice["number"],
                "by": user["username"],
                "reason": reason.strip(),
            }
        )

    STORE.mutate(apply)
    return RedirectResponse(
        f"/invoices/{invoice['id']}?notice=Invoice+placed+on+hold", status_code=303
    )


# ---------------------------------------------------------------------------
# sandbox control plane -- used by tests and the demo script, not by the agent
# ---------------------------------------------------------------------------


@app.post("/_sandbox/reset")
async def sandbox_reset() -> JSONResponse:
    STORE.reset()
    return JSONResponse({"ok": True, "detail": "sandbox reset to seed state"})


@app.post("/_sandbox/chaos")
async def sandbox_chaos(payload: dict) -> JSONResponse:
    """Queue faults, e.g. ``{"key": "erp.invoice.approve", "fault": "503", "times": 1}``."""
    if "key" in payload:
        STORE.queue_fault(payload["key"], str(payload.get("fault", "503")), int(payload.get("times", 1)))
    else:
        STORE.set_chaos(payload)
    return JSONResponse({"ok": True, "chaos": STORE.chaos()})


@app.get("/_sandbox/state")
async def sandbox_state() -> JSONResponse:
    return JSONResponse(STORE.load())


@app.get("/_sandbox/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True, "service": "nexus-erp"})
