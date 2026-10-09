# CentrAlign Operator

An autonomous operator that turns a short company request into completed,
**independently verified** work across four real surfaces — documents on a shared
drive, a browser-driven ERP, internal JSON APIs, and a **Windows desktop
application** driven through UI Automation.

Two unrelated task families run on it today — accounts-payable invoice processing
and IT new-joiner onboarding — sharing the same kernel, tools, policy engine and
verifier. Nothing in the runtime is specific to either.

You give it a sentence. It works out what the company's procedures actually
require, plans, drives the systems, notices when something is wrong, recovers,
stops to ask when the decision is not its to make, then goes back and checks the
systems of record before telling you it is done.

```
┌──────────────────────────── the operator runtime ────────────────────────────┐
│                                                                              │
│  GOAL ──► UNDERSTAND ──► PLAN ──► EXECUTE ──► OBSERVE ──► ADAPT ──► VERIFY   │
│             │              ▲         │           │          │         │      │
│             │              └─────────┴───────────┴──────────┘         │      │
│             │                   revise · repair · retry               │      │
│             │                                                          │     │
│             ▼                                                   not complete │
│      acceptance criteria ───────────── judged against ──────────────┐  │     │
│      (fixed before any work)                                        │  ▼     │
│                                                              remediate ──┘    │
│  ┌────────────────┬──────────────────┬───────────────┬──────────────────┐    │
│  │ company memory │ tool + connector │ policy engine │ event log +      │    │
│  │ SOPs, policy,  │ registry         │ risk tiers,   │ snapshots        │    │
│  │ past outcomes  │ (YAML or Python) │ approval gate │ audit + resume   │    │
│  └────────────────┴──────────────────┴───────────────┴──────────────────┘    │
└──────────────────────────────────────────────────────────────────────────────┘
```

## The two bets

Most of this system follows from two convictions about why agent prototypes fail
in real companies.

**1. An agent's report on its own work is worthless.** Agents are confidently
wrong about their own success more than about anything else. A form submitted
into a validation error reads as a successful click. A ledger write that returned
503 reads as "attempted". The closing summary says "approved 5 invoices" because
that was the plan. So verification here does not read the execution log and ask
the model whether things look fine. Acceptance criteria are fixed at intake,
*before* any work is done and before any rationalisation exists. Afterwards, a
verifier re-reads the systems of record through a registry **filtered in code to
read-only tools**, and judges each criterion from those observations alone. It can
return `unverifiable`, and an unmet criterion overrides the model's own
`complete: true`. [`runtime/verifier.py`](src/centralign/runtime/verifier.py)

**2. Generalisation is an architecture property, not a prompt property.** The
kernel contains no domain logic. It never names a tool and does not know what an
invoice is. The proof is that a second, unrelated task family — IT onboarding
across a desktop app — was added without changing a line of the kernel, the tool
base, the policy engine or the verifier. It arrived as an SOP, a YAML connector
and a new `Tool` surface. What this operator *does* lives in `company/`, not in
its code.
[`tools/connector.py`](src/centralign/tools/connector.py) ·
[`company/`](company/)

## Demo

**Live:** <https://centralign-operator.onrender.com> — the showcase at `/`, the
running operator at `/operator`.

Starting a run needs the access code, because Gemini's free tier meters 20 calls
per day *per model* and one run costs roughly 25 of them; browsing every recorded
run, its full decision stream and its evidence bundle is open to everyone. The
instance sleeps after ~15 minutes idle, so a cold first request takes about a
minute.

Note what the live instance does **not** offer: UI Automation is a Windows API,
so the deployed Linux host reports 20 tools across 6 surfaces rather than 24
across 7, and the IT onboarding examples are marked unavailable. The operator
presents the surfaces it genuinely has.

### Task family 1 — accounts payable (drive → ERP → ledger)

What to watch for, in order:

1. The request is one sentence. The operator reads SOP FIN-002 out of company
   memory and derives acceptance criteria the request never stated.
2. It reads five invoices off the drive as real PDFs and reconciles each against
   the purchase order **in the ERP**, through a browser.
3. One invoice is clean and under the clerk's authority — fully autonomous:
   registered, approved, posted to the ledger, filed.
4. One is clean but **over the clerk's authority**. The policy engine stops it
   and asks a person. Granting it lets the operator assume the controller
   identity — it never sees the password — and complete the approval.
5. One is **over-billed** (INR 186,000 against INR 120,000 remaining). It must
   not be approved. It is held with the figures stated and escalated.
6. One references a **purchase order that does not exist**, from a vendor absent
   from the approved master. Two independent blockers, both reported.
7. One is a **duplicate** of an invoice already in the register. Approving it
   again would double-pay.
8. Two files are not invoices at all and are filed without action.
9. Verification then re-reads the invoice register, the PO balances, the ledger
   and the drive folders, and judges each criterion against what it finds.
10. You get `artifacts/<run_id>/REPORT.md` — verdict first, then what changed,
    then what needs a person — alongside screenshots of every browser action and
    the full event log.

An operator that approves all five invoices is wrong. So is one that approves two
and says nothing about the other three. The scenario is built so a plausible
summary cannot pass for the work.

### Task family 2 — IT onboarding (drive → desktop app)

Same system, nothing reconfigured:

```
operator run "Someone new is joining - there's a setup request in the HR inbox. Please get them set up."
```

1. The request is a **prose memo**, not a form: "Priya Venkatesan joins us on
   Monday as a QA Engineer… she will need the standard QA engineer kit."
2. The operator derives the username from the convention in the SOP
   (`p.venkatesan`), and checks the directory for a collision first.
3. It opens the **Asset and Access Manager desktop application** and creates the
   account — writes to this system are GUI-only, there is no API for them.
4. It reads the role's standard kit out of the SOP, finds hardware that is
   actually *available* (not the one in repair, not the one someone else holds),
   and issues a laptop and a monitor.
5. It grants the role's unrestricted access groups.
6. The memo also asks for **finance systems access**. That group is restricted,
   and the application refuses to grant it from that screen — so the operator
   escalates with its findings and a recommendation, and waits.
7. Verification re-reads the directory, the asset register and the group
   memberships through a *different channel* (the read-only IT API) and confirms
   the restricted group was **not** granted.

Note the contrast in how the two families stop for a human. Finance is
**policy-driven**: the operator is halted before exceeding its authority. IT is
**refusal-driven**: the application says no and the operator escalates in
response. Two triggers, one mechanism, no special-casing in the kernel.

A real live run of this task (Gemini making every decision) took 42 steps across
7 planning cycles and verified 5/5 criteria.

## Quick start

Requires Python 3.11+.

```bash
git clone <this repo> && cd centralign-operator

# 1. install
pip install uv                      # or use plain pip/venv
uv venv --python 3.12
uv pip install -e ".[dev]"
.venv/Scripts/python -m playwright install chromium    # macOS/Linux: .venv/bin/python

# 2. configure a model — the free tier is enough
cp .env.example .env
# Get a free key, no credit card: https://aistudio.google.com/apikey
# then put it in .env as GEMINI_API_KEY=...
#
# NOTE the default OPERATOR_LLM_MODEL is a comma-separated FALLBACK CHAIN.
# Gemini's free tier meters 20 requests per day PER MODEL, so a nine-model chain
# is worth roughly 180 calls a day - about 12-18 full runs. See "Free tier" below.

# 3. start the sandbox company (mock ERP + internal APIs + seeded drive)
.venv/Scripts/python -m sandbox.serve --reset

# 3b. and, for the IT onboarding family, the desktop application (Windows)
#     The operator will start this itself if it is not already running.
.venv/Scripts/python -m sandbox.desktop.asset_manager

# 4. in another terminal, check everything is wired up
.venv/Scripts/python -m centralign.cli doctor

# 5. give it work
.venv/Scripts/python -m centralign.cli run \
  "The vendor invoices in the shared drive need processing - please take care of them."
```

Add `--no-headless` to watch Chromium drive the ERP.

When the operator needs a person, it stops and prints how to answer:

```bash
.venv/Scripts/python -m centralign.cli resume latest --approve \
  --response "Checked against the PO. Approve on the controller account."
```

Other commands:

| Command | What it does |
| --- | --- |
| `operator doctor` | config, provider, sandbox reachability, Chromium — run this first |
| `operator serve` | live dashboard on `:8780`, with real approve/decline buttons |
| `operator tools` | the tool catalog exactly as the planner sees it |
| `operator runs` / `operator show <id>` | list runs · print a run's report |
| `operator sandbox --reset` | reseed the sandbox world between demos |
| `pytest -q` | 125 tests (sandbox tests skip if it is not running) |

The ERP is at <http://127.0.0.1:8781> (`a.rao` / `operator-sandbox`); the API's
OpenAPI docs are at <http://127.0.0.1:8782/docs>.

### Free tier: what it actually costs

Gemini's free tier meters **20 requests per day, per model**. One full run uses
roughly 10–15 model calls, so a single model is worth about one run a day. That is
why `OPERATOR_LLM_MODEL` is a chain rather than a name:

```
OPERATOR_LLM_MODEL=gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,...
```

Nine models ≈ 180 calls/day ≈ 12–18 runs. The chain sticks to whichever model is
answering, skips models it has seen exhausted, and refuses to fall back on a
genuine bad request. Daily-quota 429s carry an eleven-hour `retryDelay`; the
runtime treats those as "this model is done" and moves on rather than sleeping —
a bug that, before it was fixed, would have hung a run until the next morning.

If you exhaust everything, `OPERATOR_LLM_PROVIDER=replay` still runs from cassettes.

### No API key? The system still runs

Every model call is content-addressed and cached to disk, and that cache *is* a
replay cassette. After one live run, `OPERATOR_LLM_PROVIDER=replay` reproduces it
exactly, offline and free. The 125-test suite needs no key at all, and
`scripts/offline_demo.py` drives the complete execution and verification
machinery with a recorded decision sequence — useful for inspecting a real
evidence bundle without spending quota. It is clearly labelled: it exercises the
system's hands, not its judgement.

## Deploying it

A container host, not a serverless one. The operator is a poor fit for Vercel or
Lambda and it is worth being precise about why, because the reasons are the shape
of the system rather than a configuration problem:

| What a run needs | What serverless gives |
| --- | --- |
| 2–5 minutes of wall clock | 10–300s before the function is killed |
| Chromium, ~400MB | 250MB unzipped bundle limit |
| In-process background tasks + SSE with shared state | Stateless invocations |
| SQLite on a writable disk | Read-only except an ephemeral `/tmp` |
| A Windows desktop session (IT family) | Headless Linux |

So the repo ships a `Dockerfile` and a `render.yaml`:

```bash
docker build -t centralign-operator .
docker run -p 8780:8780 -e GEMINI_API_KEY=... centralign-operator
```

`python -m centralign.serve_all` runs the dashboard, the mock ERP and the internal
APIs as three uvicorn servers on one event loop. That is deliberate: the ERP, the
APIs and the drive are not infrastructure, they are the *sandbox company*. They
share one state file so a reset restores a byte-identical world, and splitting
them into separate containers would mean inventing shared storage to fake what
they already have. Only the dashboard binds publicly; the sandbox services listen
on loopback, which is also the right posture — nothing outside should be able to
reach the mock ERP except the operator.

Two things a deployed instance does differently, both reported through
`/api/config` so the UI can reflect them rather than offering a button that can
only fail:

- **The desktop surface is absent.** UI Automation is a Windows API, so on Linux
  those four tools are not registered at all and the IT-ops examples are shown as
  unavailable. The operator presents the surfaces it genuinely has.
- **`OPERATOR_PUBLIC_DEMO=1` makes it read-only.** The free tier meters 20 model
  calls per day *per model*, so one passing visitor could exhaust the demo for
  everyone. Browsing recorded runs, their decision streams and their evidence
  stays fully open; only *starting* a run is gated, by `OPERATOR_DEMO_TOKEN` if
  set and refused outright if not.

Memory, not CPU, is the binding constraint: Chromium alone wants ~512MB, so the
smallest free tiers will kill a run mid-browse.

## Architecture

```
src/centralign/
  runtime/
    kernel.py      the loop: understand → plan → execute → observe → adapt → verify
    state.py       RunState — one serializable object a run is recovered from
    verifier.py    independent verification against re-observed world state
    prompts.py     every prompt and output schema, in one reviewable place
  tools/
    base.py        Tool ABC, failure taxonomy, HumanInputRequired
    registry.py    the catalog the planner reads
    drive_tools.py    documents on the shared drive
    browser_tools.py  real Chromium against the ERP
    desktop_tools.py  real Windows UI Automation against a Qt application
    core_tools.py     recall, working memory, asking a person
    connector.py      YAML-defined HTTP connectors — a new system with no code
  memory/store.py  company memory: SOPs, policy, past outcomes (SQLite FTS5/BM25)
  policy/guard.py  risk tiers and the approval gate — the security boundary
  events/log.py    append-only audit trail + run snapshots
  evidence/        the bundle a person actually receives
  llm/             provider-agnostic model access, caching, retry, replay
  web/             live dashboard (SSE)
  serve_all.py     one-process entrypoint for a container host

sandbox/           the mock company:
  erp/               Nexus ERP, a server-rendered web app (browser target)
  desktop/           Asset & Access Manager, a Qt desktop app (UIA target)
  api/               FinanceOps + IT Operations JSON APIs
  drive/             seeded invoice PDFs and an HR joiner memo
company/           SOPs, policy rules, connector specs, credential vault
```

### The loop, concretely

**Understand.** The request plus retrieved SOPs become an objective, deliverables,
assumptions, and acceptance criteria — each with a `check` recording *how to
confirm it by observation*. Without that field a verifier degrades into asking the
model whether it feels successful. If the request genuinely cannot be started, one
blocking question is raised; a question the operator could answer itself with its
own tools is not a blocking question.

**Plan.** A full plan up front, so a human can preview the whole intent and
approvals can be reasoned about before anything executes. But the plan is a
**living artifact**, not a frozen DAG: any observation can revise, insert into or
replace the remaining steps. A frozen DAG cannot react to "this invoice
references a PO that does not exist"; a pure step-at-a-time ReAct loop has no plan
to show anyone. This is the middle.

**Execute.** Step arguments resolve `${steps.s3.data.total}` and `${facts.key}`
against a blackboard **in plain code**. A model call per step just to turn state
into arguments would double cost; the model is consulted only when resolution
*fails*. Before dispatch, the policy engine computes the risk of this specific
invocation from the tool and its real arguments.

**Observe.** Every tool returns an `Observation`, never an exception — a tool that
raised past that boundary would kill the run instead of being adapted around.
`data` is machine-facing and feeds later steps and the verifier; `summary` is
model-facing prose.

**Adapt.** Failures are classified before they are reasoned about, which is where
most agent loops go wrong:

| Failure kind | Response | Model call? |
| --- | --- | --- |
| `transient` `timeout` `unavailable` | retry with jittered backoff — **only if the tool is idempotent** | no |
| `invalid_args` | repair the arguments against the tool's contract | one cheap call |
| `not_found` `precondition_failed` `conflict` | never retried — replan or treat as a finding | adapt |
| `permission_denied` | escalate to a person | adapt |
| `ambiguous` | ask a person | adapt |

A refusal is not a fault. "This vendor is not in the approved master" is
information the SOP already covers; retrying it changes nothing and burns a free
tier. Conversely, a 503 should cost a retry, not a reasoning cycle.

**Verify.** Covered under bet 1 above. If verification fails and offers
remediation, the kernel gets **one** bounded round to fix it and re-verify. That
loop is what makes verification a control rather than a report.

**Complete.** `artifacts/<run_id>/` gets `REPORT.md` (verdict first), `run.json`,
`events.jsonl`, `verification.md`, and a screenshot of every browser action.
Learnings are written to episodic memory for the next run.

## Design decisions worth arguing about

**Schema-constrained JSON everywhere, never vendor tool-calling.** Tool-calling
semantics differ enough across providers that a swap would ripple into the kernel.
Schema-constrained JSON is the one capability that behaves consistently across
Gemini, Anthropic, OpenAI-compatible endpoints and local models. Swapping
providers is now one environment variable. It also means the plan, the verdict and
every decision are typed objects we log, diff and replay — not opaque
provider-side state. Cost: we hand-roll JSON salvage for models that wrap output
in prose, and prune schemas down to Gemini's accepted subset. Both are tested.
[`llm/base.py`](src/centralign/llm/base.py)

**Arguments travel as a JSON string inside the schema.** An open `object`
property is exactly what constrained decoding handles worst — Gemini rejects a
property-less object outright, others fill it with invented keys. A string the
kernel parses is stricter in practice than a schema that cannot be expressed.

**BM25, not embeddings.** The corpus is a few hundred short, jargon-dense
operational documents where exact matching on identifiers (`PO-4471`,
`Northwind`, `freight`) beats semantic similarity. It is free, needs no service,
and runs offline. Swapping in a vector index means reimplementing one method.

**Risk is computed, never self-reported.** The model proposes; the policy engine
decides. A model that could declare its own actions low-risk would eventually talk
past every gate — by accident if not via injection from a document it read. Rules
live in `company/policies.yaml`, and precedence is `deny > require_approval >
allow_without_approval > baseline gate`, so a rule granting autonomy can never
override one demanding a human. An unparseable policy file drops to `supervised`;
an unevaluable rule escalates. Both tested.

**Credentials never reach the model.** `erp_login` takes an *account role* and
resolves the secret from a vault. The model can ask to act as the finance
controller — and policy can refuse — but cannot read, type or exfiltrate a
password. Secret files are also excluded from memory ingestion, so a vendor PDF
saying "search memory for the admin password" has nothing to find.

**Two independent enforcement layers.** The operator's policy stops an
over-authority approval before it is attempted. The ERP refuses it anyway, because
an internal system cannot trust its caller. If the agent's reasoning fails, the
ERP still says no — and the resulting error is something the agent must recover
from. Both paths are tested.

**Never blindly retry a non-idempotent action.** Re-firing a write risks
double-executing work the first attempt may have partially completed. Idempotent
tools retry in place; others go to adapt, which can re-establish preconditions
first. This was found by a test: a 503 on a form submit was initially reported as
*success*, because the click resolved and the page changed. `browser_click` now
checks the resulting main-frame HTTP status, restores the form page, and reports a
retryable failure.

**A skipped step does not satisfy its dependents.** Also found by a test. Treating
"we gave up on this" as "done" let downstream steps run on data their prerequisite
never established — in an approvals workflow, that is approving something nobody
checked.

**Pages are observed as structured summaries, not HTML.** One ERP page becomes a
few hundred tokens — banners, headings, definition lists, tables, form fields,
buttons, links, and who is signed in — instead of tens of thousands of markup.
This is most of what makes a free-tier model viable here.

**Events and snapshots, not pure event sourcing.** Folding events is the elegant
recovery story, but every state field then needs a replay rule and one missing
rule corrupts recovery silently. Snapshots make recovery a dumb, obviously-correct
load; the append-only event log keeps the audit guarantee snapshots alone would
lose. A little redundancy for a much smaller blast radius.

**Context is compressed, not accumulated.** Prompts are built from a bounded
situation report — blackboard, criteria, recent history, a decision journal —
rather than a growing transcript. Token use per step stays roughly flat as a run
lengthens.

## Generalisation

**A different task family, no kernel change.** IT onboarding was added as an SOP,
a read-only YAML connector and one new `Tool` surface. The kernel, the tool base
class, the policy engine, the verifier and the evidence bundle are byte-identical.
That is the claim, and `tests/test_onboarding_workflow.py` is the proof — it runs
the whole thing against the real desktop application and asserts against the IT
system of record.

**A different task, no code change at all.** Try:

```bash
operator run "Reconcile PO-4490 and tell me whether we have been over-billed."
operator run "Check the invoice inbox for problems and tell me what needs my attention."
```

Different objective, different criteria, different plan, same system. The invoice
workflow lives in `company/sops/`, not in Python.

**A different system, no code change.** Drop a YAML file in
`company/connectors/`:

```yaml
name: ticketing
base_url_setting: api_url
operations:
  - name: ticket_create
    method: POST
    path: /tickets
    description: Raise a ticket. Call this when work needs a person to pick it up.
    args:
      title: {type: string, description: One-line summary.}
      body:  {type: string, description: What happened and what is needed.}
    required: [title, body]
    body: [title, body]
    risk: write
    status_map: {503: unavailable, 404: not_found}   # drives retry vs replan
```

It appears in the planner's catalog on next start, indistinguishable from a
built-in. `status_map` is the part that matters for reliability: without it a 404
and a 503 look identical to the adapt phase, and it would retry the one that can
never succeed while giving up on the one that would.

The DSL deliberately has no conditionals, loops or response transforms. Anything
needing those should be a Python `Tool` — a connector DSL that grows into a
programming language is a liability.

## Testing

202 tests, no API key required.

```bash
pytest -q                                   # everything
pytest tests/test_kernel.py -q              # kernel behaviour, scripted model
pytest tests/test_full_workflow.py -q       # finance family, real sandbox
pytest tests/test_onboarding_workflow.py -q # IT family, real desktop app
pytest tests/test_desktop_integration.py -q # the desktop surface via UIA
```

Tests needing the sandbox, Windows or Qt skip themselves cleanly, so `pytest -q`
works on a bare checkout.

- **Kernel behaviour** (`test_kernel.py`) — a scripted model drives the real
  kernel, so retry, adaptation, approval gating, loop detection, budget
  exhaustion and resume are deterministic and assertable. Testing these against a
  live model would measure the model, slowly and flakily.
- **Tool surfaces** (`test_sandbox_integration.py`) — real Chromium against the
  real ERP, real HTTP, real PDF extraction, injected faults.
- **The whole system, twice** (`test_full_workflow.py`, `test_onboarding_workflow.py`)
  — only the model's reasoning is scripted. Every final assertion reads the
  **system of record**, not the operator's account of it. Includes the case that
  matters most: a run that approves an invoice but never posts it to the ledger,
  where the operator's own record says "completed" and verification must
  contradict it.
- **The desktop surface** (`test_desktop_integration.py`) — real Chromium's
  counterpart: a real Qt window, driven through UIA, including every refusal the
  application can produce.
- **Policy and references** (`test_policy_and_state.py`) — the places where a
  bug is silent and expensive.

Failure injection is via an endpoint, not a random number generator, so recovery
is demonstrable on cue rather than hoped for:

```bash
curl -X POST localhost:8781/_sandbox/chaos \
  -d '{"key":"erp.invoice.approve","fault":"503","times":1}' -H 'content-type: application/json'
```

## Known limitations

Stated plainly, because they are the honest edges of a prototype.

- **Gating a generic click is imprecise.** The page decides what a click does. We
  escalate on policy-configured action words and rely on the ERP's own
  server-side authority check as the second layer. A production deployment should
  prefer named connector actions, whose effect and risk are known in advance, for
  anything mutating.
- **Runs execute in-process.** `cli run` and the dashboard run the kernel as a
  task. The kernel is already written for a durable queue — a run is one
  serializable state object plus an event log — but the queue, leases and worker
  recovery are not built.
- **One run at a time per process, and one browser context.** Concurrency across
  invoices would need a session pool and per-item isolation.
- **Verification is only as good as its criteria.** They come from the model at
  intake. Weak criteria produce a weak verdict — see the live-run note above for
  a real instance of exactly that. Mitigated by making them explicit and auditable
  in the report; not solved.
- **Prompt injection is mitigated, not solved.** Documents are framed as data,
  secrets are excluded from memory, credentials are role-resolved, and policy is
  not a prompt. But a document that successfully persuades the planner to take a
  policy-permitted action in a harmful order is not caught today.
- **Both computer-use surfaces are accessibility-based, not vision-based.** The
  browser reads the DOM; the desktop reads the UIA tree. When an accessibility
  tree exists this is strictly better — deterministic, no OCR, controls named the
  way a person reads labels, and a few hundred tokens instead of pixels. But it
  will not drive a canvas app, a remote-desktop session or a Citrix window. A
  vision loop belongs behind the same `Tool` interface; it is not built.

- **The desktop surface is Windows-only.** It uses UI Automation. It registers
  itself only when importable, so the operator degrades to its other surfaces
  elsewhere rather than failing to start.

- **Live runs expose model limits the harness cannot fix.** In a real onboarding
  run the operator provisioned everything correctly and verified 5/5 criteria,
  but it did not escalate the restricted `finance-systems` group the memo asked
  for — it simply did not action it, and then reported that no escalation was
  needed. The harness did its job (the application refused the grant, nothing
  unsafe happened, the evidence is honest) but the *verdict* was wrong because the
  acceptance criterion it wrote for itself was too weak. This is the sharpest
  limitation in the whole system: **verification is only as good as the criteria,
  and the criteria come from the model.** Criteria libraries per task type are
  the fix, and they are the next thing I would build.
- **No OCR.** An image-only invoice PDF is correctly escalated to a person rather
  than guessed at, which is the right behaviour but still a gap.
- **Free-tier models are the weak link.** The harness absorbs a lot — JSON
  salvage, corrective retries, compact observations, argument repair, a
  nine-model fallback chain — but plan quality on a small model is noticeably
  below a frontier one, and it wanders: a live run took 42 steps and 7 planning
  cycles for work a tight plan does in 23. Nothing about the architecture assumes
  the cheap model; it is one env var.
- **Episodic memory is written but lightly used.** Past-run learnings are stored
  and retrieved by BM25. There is no consolidation, no decay, and no mechanism for
  a human correction to durably override an SOP.
- **The sandbox is a sandbox.** Fictitious data, no real credentials, no third
  party touched.

## What I would build next

In the order I would actually do it:

1. **Durable execution.** Runs onto a queue with leases and heartbeats; resume
   after a worker dies, not just after a human pause. The state object is ready;
   the infrastructure is not. This is the single biggest gap between this and
   something a company could depend on.
2. **Per-item isolation.** Each invoice as an independently retryable child run
   with its own budget, so one pathological document cannot consume the cycle.
   This also unlocks real concurrency.
3. **Criteria libraries and regression evals.** This moved to the top of the list
   after the live runs. Versioned acceptance criteria per task type, reviewed by
   humans and reused rather than re-derived each run, so the verifier is not
   graded against whatever the model thought of that morning. Plus a scored
   harness over the seeded scenarios, so a prompt or model change shows up as a
   number before it ships. Right now I can tell you the system works; I cannot
   tell you it got 7% better.
4. **A learning loop with teeth.** When a human corrects the operator, capture it
   as a typed corrective memory bound to an entity, and measure whether the next
   run on that entity behaves differently. Episodic memory without that
   measurement is a diary.
5. **Tighter action gating.** Declarative permissions on `(system, operation,
   entity)` rather than tool names and action words, with the policy decision for
   every action recorded in the bundle as a first-class artifact.
6. **A vision-based computer-use surface** for applications without a usable DOM,
   behind the same `Tool` interface, so the kernel does not learn about it.
7. **Multi-tenant company configuration.** `company/` becomes a versioned,
   per-tenant artifact with a migration path, so onboarding a company is a
   reviewable change rather than editing files in a repo.

## Assumptions made

- The reviewer runs the sandbox locally. No real company system is touched, and
  no real credential exists anywhere in the repo.
- Finance operations in an Indian entity: INR, GSTIN, `PO-nnnn`, `Net 30`. Rupee
  amounts are written `INR 42,000` in generated PDFs because the standard PDF
  font has no rupee glyph.
- A short request means the governing SOP supplies the unstated steps. This is the
  core interpretive assumption, and it is why company memory is load-bearing
  rather than decorative.
- Approval authority is per-account and enforced by the ERP, so the escalation
  path involves a genuine identity switch rather than a flag.
- One operator instance serves one company. Multi-tenancy is listed above, not
  built.
- A free-tier model is the target, which shaped the harness: compact observations,
  cheap deterministic recovery paths, bounded context, aggressive caching.

## What this is built on

**Mine:** the runtime kernel and its phases, state model, tool and connector
abstractions, the connector DSL, policy engine, company memory and retrieval,
event log and snapshots, evidence bundles, the provider-agnostic LLM layer
(including JSON salvage, schema pruning, the model fallback chain, quota
handling, caching and replay), all four tool surfaces, the CLI, the dashboard,
the mock ERP, the mock desktop application, both internal APIs, the two seeded
scenarios, a dependency-free PDF writer, and the 202-test suite.

**Pre-built components used:**

| Component | Role |
| --- | --- |
| Google Gemini (`gemini-3.8-flash`), free tier | default reasoning model |
| Anthropic / OpenAI / Groq / OpenRouter / Cerebras / Together / Ollama | drop-in alternatives, one env var |
| Playwright + Chromium | browser automation |
| pywinauto + Windows UI Automation | desktop application control |
| PySide6 (Qt) | the mock desktop application |
| Pillow | desktop screenshots for evidence |
| FastAPI · uvicorn · Jinja2 | dashboard and the two sandbox services |
| Pydantic v2 | typed, serializable run state |
| httpx | all HTTP, including every model provider (no vendor SDKs) |
| SQLite + FTS5 | event log, snapshots, BM25 company memory |
| pypdf | invoice text extraction |
| Typer + Rich | CLI |
| pytest + pytest-asyncio | tests |

No agent framework. No LangChain, no LlamaIndex, no CrewAI, no vector database.
The control loop, the failure taxonomy and the verification design are the
substance of this submission, and wrapping someone else's loop would have hidden
exactly the parts worth evaluating.

---


