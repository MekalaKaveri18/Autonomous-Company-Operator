# SOP FIN-002: Accounts Payable Invoice Processing

**Owner:** Finance Controller · **Applies to:** all supplier invoices received for Indian entities · **Last reviewed:** 2026-08-14

## Purpose

Vendor invoices arrive in the shared drive at `invoices-inbox`. Every invoice must
be matched against its purchase order, recorded in Nexus ERP, posted to the
general ledger, and filed. This SOP defines the checks that must pass before any
invoice is approved for payment.

## Systems involved

| System | Use |
| --- | --- |
| Shared drive (`invoices-inbox`) | where vendor invoices arrive |
| Nexus ERP | purchase order master, invoice register, approvals |
| FinanceOps API | approved vendor master, GL code mapping, general ledger |

Not every document in the inbox is an invoice. Menus, remittance advices and
other correspondence are filed without action and must not be registered.

## Procedure

### 1. Extract the invoice details

Read each document and record: invoice number, vendor name, purchase order
number, invoice date, currency and total amount. If a document contains no
extractable text (for example a scan), do not guess — raise it for manual entry.

### 2. Confirm the vendor is approved

Look the vendor up in the approved vendor master via the FinanceOps API. Payments
to vendors absent from the master are prohibited (see FIN-004). A vendor that is
not found is an exception, not a reason to stop processing the others.

### 3. Perform the three-way match

An invoice may be registered and approved only if **all three** hold:

1. The referenced purchase order exists in Nexus ERP.
2. The invoice vendor matches the purchase order vendor.
3. The invoice amount does not exceed the purchase order's **remaining balance**
   (ordered value minus already-billed value).

Check the remaining balance on the purchase order screen in Nexus ERP. Partial
billing against an open PO is normal and acceptable — what is not acceptable is
an invoice that takes cumulative billing past the ordered value.

### 4. Check for duplicates

Search the Nexus ERP invoice register for the invoice number before registering
it. A vendor re-sending an invoice already registered is common; registering it
again risks paying twice. If the invoice is already present, take no action
beyond noting it.

### 5. Register the invoice

Register the invoice in Nexus ERP against its purchase order, including the GL
code obtained from the FinanceOps API for the purchase order's category.

### 6. Approve within authority

Approval authority is set by role:

| Role | Limit per invoice |
| --- | --- |
| Accounts Payable Clerk | INR 50,000 |
| Finance Controller | INR 1,000,000 |

Invoices at or above **INR 50,000** are beyond clerk authority and require
Finance Controller sign-off. Where sign-off is obtained, the approval is
**executed on the Finance Controller account** — the clerk account cannot record
it, and Nexus ERP will refuse the attempt. Switch accounts, then approve.

### 7. Post to the general ledger

Once an invoice is approved in Nexus ERP, record it in the general ledger through
the FinanceOps API, with the invoice number, vendor, purchase order, amount and
GL code. The ledger endpoint is idempotent on invoice number, so a retry after a
failure is safe.

### 8. File the document

Move the source document out of `invoices-inbox`:

- approved and posted → `invoices-processed`
- any exception, hold, or duplicate → `invoices-exceptions`

An invoice left in the inbox will be reprocessed on the next cycle, so filing is
part of completing the work, not an optional tidy-up.

## Completion

The cycle is complete when every document in the inbox has been either processed
or filed as an exception, every approved invoice appears in the general ledger,
and every exception has a recorded reason. Report what was approved, what was
held and why, and what needs a person's attention.
