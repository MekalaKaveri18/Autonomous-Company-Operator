# SOP IT-001: New Joiner Setup

**Owner:** IT Operations · **Applies to:** every new employee · **Last reviewed:** 2026-09-02

## Purpose

People Operations file a setup request in the shared drive at `hr-inbox` when
someone joins. The request is a memo, not a form, so the details have to be read
out of it. This SOP defines what "set up" means so that a joiner can work on
their first morning.

## Systems involved

| System | Use |
| --- | --- |
| Shared drive (`hr-inbox`) | where joiner requests arrive |
| Asset and Access Manager (desktop application) | the IT system of record for accounts, hardware and access groups. **All changes are made here.** There is no API for writes. |
| IT Operations API | read-only. Use it to check and to confirm, not to change. |

## Procedure

### 1. Read the request

Take from the memo: full name, start date, job role, and department. Note
anything the requester has asked for specifically, including anything unusual.

### 2. Derive the username

The convention is first initial, a dot, then the surname, all lower case:
Priya Venkatesan becomes `p.venkatesan`. Check the directory first — if the
username already exists, do not create a second account. An existing account for
a *different* person means the convention has collided, which needs People
Operations to decide; raise it rather than inventing a variant.

### 3. Create the account

Create the account in the Asset and Access Manager with the full name, username,
role and department from the request, and the start date.

### 4. Issue the standard hardware kit

Kit by role:

| Role | Standard kit |
| --- | --- |
| QA Engineer | 1 laptop, 1 monitor |
| Software Engineer | 1 laptop, 1 monitor |
| Accounts Payable Clerk | 1 laptop |
| Finance Controller | 1 laptop, 1 monitor, 1 phone |
| Procurement Lead | 1 laptop, 1 phone |

Assign only hardware whose status is **available**. Hardware marked `in_repair`
must not be assigned, and hardware already assigned to someone else must not be
taken from them. If no suitable item is available, record the shortfall and raise
it with IT Operations rather than leaving the joiner without a recorded outcome.

### 5. Grant the standard access for the role

| Role | Access groups |
| --- | --- |
| QA Engineer | `eng-general`, `qa-automation`, `vpn-standard` |
| Software Engineer | `eng-general`, `vpn-standard` |
| Accounts Payable Clerk | `vpn-standard`, `finance-systems` |
| Finance Controller | `vpn-standard`, `finance-systems` |
| Procurement Lead | `vpn-standard` |

### 6. Anything restricted needs approval first

Groups marked **Restricted** in the Asset and Access Manager — currently
`finance-systems` and `prod-deploy` — require Finance Controller approval and
cannot be granted from the Access Groups screen. If a request asks for one, or a
role's standard access includes one:

1. grant everything else first, so the joiner is not blocked on it;
2. obtain approval, stating who the joiner is, which group, and why it was asked
   for;
3. record the outcome either way.

A restricted group that was requested and not granted is an **escalation**, not a
silent omission. Never record it as complete.

### 7. File the request

Move the memo from `hr-inbox` to `hr-completed` once every item above has an
outcome. A request left in the inbox will be picked up again next cycle.

## Completion

The joiner is set up when the account exists, the standard kit for their role is
assigned to them, every unrestricted group for their role is granted, every
restricted group that was asked for has either approval or a recorded
escalation, and the request has been filed. Report what was done, what is waiting
on an approval, and anything that could not be provided.
