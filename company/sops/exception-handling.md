# SOP FIN-007: Handling Accounts Payable Exceptions

**Owner:** Finance Controller · **Last reviewed:** 2026-08-14

Exceptions are normal. An invoice cycle in which nothing is held is unusual, and
an operator that forces every invoice through to approval is a bigger problem
than one that holds too many. The governing principle: **never approve an invoice
you cannot fully substantiate, and never silently drop one you could not
process.**

## Exception types and required action

### Purchase order not found

The invoice references a PO that does not exist in Nexus ERP. Do not register the
invoice — Nexus ERP requires a valid PO. Place the document in
`invoices-exceptions` and escalate to Procurement, quoting the PO number the
vendor used. Common causes: the vendor quoted their own reference, the PO was
never raised, or the invoice belongs to another entity.

### Vendor not in the approved master

Payment is prohibited under FIN-004 until Procurement onboards the vendor. File
as an exception and escalate. If the invoice *also* has no valid PO, report both
findings — a single invoice can have more than one blocker, and reporting only
the first wastes a cycle.

### Amount exceeds remaining purchase order balance

This is potential over-billing and is the most serious exception in this
process. Do **not** approve. Register the invoice if the PO is valid, place it on
hold in Nexus ERP with the variance stated, and escalate to the Finance
Controller with:

- the invoice amount,
- the PO ordered value and the amount already billed,
- the resulting remaining balance,
- the excess.

State the figures. "Amount mismatch" is not a usable escalation.

### Duplicate invoice

The invoice number already exists in the Nexus ERP register. Take no action on
the record. File the document to `invoices-exceptions` noting it as a duplicate
of the existing entry. Do not register, do not approve, do not post to the
ledger.

### Beyond approval authority

Not strictly an exception — it is the normal path for larger invoices. Obtain
Finance Controller sign-off, then execute the approval on the Finance Controller
account as set out in FIN-002 §6.

### Document is not an invoice

File it out of the inbox without creating any record. Do not escalate; this needs
nobody's attention.

## Escalation content

An escalation must let the recipient decide without re-doing the work: what the
document is, what was checked, what was found, which figures conflict, and what
you recommend. Attach the evidence you gathered.

## If a system is unavailable

Retry a failed read or write; transient failures in Nexus ERP and the FinanceOps
API are common. If it continues to fail, stop work on that item, preserve what
has been established so far, and report the system as unavailable rather than
recording a false outcome. A partially-processed invoice reported honestly is
recoverable; one reported as complete is not.
