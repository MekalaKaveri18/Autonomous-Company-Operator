"""Command line interface.

Live event output is not decoration. An autonomous system that only speaks at the
end is impossible to trust or debug; watching the plan form, the steps run and a
failure get adapted around is how you tell reasoning from recitation.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .bootstrap import build_operator
from .config import SETTINGS
from .events.log import Event
from .runtime.state import RunState, RunStatus

app = typer.Typer(
    add_completion=False,
    help="CentrAlign operator: turn a company request into completed, verified work.",
)
console = Console()

_QUIET_EVENTS = {"verification.probe", "step.args_repaired"}

_EVENT_STYLE = {
    "run.created": ("bold cyan", "run"),
    "intake.started": ("cyan", "understand"),
    "intake.completed": ("bold cyan", "understand"),
    "plan.created": ("bold magenta", "plan"),
    "plan.revised": ("bold magenta", "replan"),
    "plan.steps_inserted": ("magenta", "repair"),
    "step.started": ("blue", "execute"),
    "step.observed": ("green", "observe"),
    "step.retrying": ("yellow", "retry"),
    "step.failed": ("red", "failed"),
    "step.skipped": ("yellow", "skip"),
    "adapt.decided": ("bold yellow", "adapt"),
    "loop.detected": ("bold red", "loop guard"),
    "policy.approval_required": ("bold yellow", "policy"),
    "policy.denied": ("bold red", "policy"),
    "human.requested": ("bold yellow", "human"),
    "human.responded": ("bold green", "human"),
    "verification.started": ("bold blue", "verify"),
    "verification.completed": ("bold blue", "verify"),
    "verification.remediation_started": ("bold yellow", "verify"),
    "budget.exceeded": ("bold red", "budget"),
    "run.aborted": ("bold red", "abort"),
    "run.completed": ("bold green", "done"),
    "run.failed": ("bold red", "failed"),
}


def _print_event(event: Event) -> None:
    if event.type in _QUIET_EVENTS:
        return
    style, label = _EVENT_STYLE.get(event.type, ("dim", event.type))
    payload = event.payload
    detail = ""

    if event.type == "intake.completed":
        detail = f"objective: {payload.get('objective', '')}\n"
        for criterion in payload.get("acceptance_criteria", []):
            detail += f"    - [{criterion['id']}] {criterion['text']}\n"
        detail = detail.rstrip()
    elif event.type in {"plan.created", "plan.revised"}:
        detail = f"v{payload.get('version')}: {payload.get('rationale', '')[:200]}\n"
        for step in payload.get("steps", []):
            detail += f"    {step['id']}  {step['tool']}  {step['intent'][:80]}\n"
        detail = detail.rstrip()
    elif event.type == "step.started":
        detail = (
            f"{event.step_id} {payload.get('tool')} risk={payload.get('risk')} "
            f"attempt={payload.get('attempt')}\n    {payload.get('intent', '')}"
        )
    elif event.type == "step.observed":
        mark = "ok" if payload.get("ok") else f"FAILED ({payload.get('error_kind')})"
        detail = f"{event.step_id} {mark} in {payload.get('duration_ms', 0)}ms\n"
        detail += "    " + str(payload.get("summary", ""))[:400].replace("\n", "\n    ")
    elif event.type == "adapt.decided":
        detail = f"{event.step_id} -> {payload.get('action')}: {payload.get('reason', '')[:240]}"
    elif event.type == "human.requested":
        detail = f"[{payload.get('kind')}] {payload.get('prompt', '')[:600]}"
    elif event.type == "verification.completed":
        detail = (
            f"complete={payload.get('complete')} confidence={payload.get('confidence')}\n"
            f"    {payload.get('summary', '')[:300]}\n"
        )
        for criterion in payload.get("criteria", []):
            detail += f"    [{criterion['id']}] {criterion['status'].upper()}: {criterion['note'][:160]}\n"
        detail = detail.rstrip()
    elif event.type == "run.completed":
        detail = f"{payload.get('status')} -- {payload.get('headline', '')}"
    else:
        detail = json.dumps(payload, default=str)[:300] if payload else ""

    console.print(f"[{style}]{label:>10}[/] {detail}")


def _summarise(state: RunState) -> None:
    verdict = state.verdict
    colour = {
        RunStatus.COMPLETED: "green",
        RunStatus.PARTIAL: "yellow",
        RunStatus.AWAITING_HUMAN: "yellow",
    }.get(state.status, "red")

    body = [f"[bold]{(state.facts.get('report') or {}).get('headline', state.objective)}[/]", ""]
    if verdict:
        body.append(
            f"verified complete: [bold]{verdict.complete}[/]  "
            f"confidence: {verdict.confidence:.0%}  "
            f"criteria met: {verdict.met_count}/{len(verdict.criteria)}"
        )
    body.append(
        f"steps: {state.budget.steps_used}  model calls: {state.budget.llm_calls_used}  "
        f"replans: {state.budget.replans_used}  elapsed: {state.budget.elapsed:.0f}s"
    )
    bundle = state.facts.get("evidence_bundle")
    if bundle:
        body.append("")
        body.append(f"evidence bundle: {bundle}")
        body.append(f"report:          {Path(bundle) / 'REPORT.md'}")

    console.print(Panel("\n".join(body), title=f"{state.status.value}  ({state.run_id})", border_style=colour))

    pending = state.pending_human()
    if pending is not None:
        console.print(
            Panel(
                f"{pending.prompt}\n\n"
                + (f"Options: {', '.join(pending.options)}\n\n" if pending.options else "")
                + "[bold]Resume with:[/]\n"
                + f"  operator resume {state.run_id} --approve --response \"...\"\n"
                + f"  operator resume {state.run_id} --reject  --response \"...\"",
                title=f"AWAITING HUMAN ({pending.kind})",
                border_style="yellow",
            )
        )


@app.command()
def run(
    goal: str = typer.Argument(..., help="What you want done, in plain language."),
    headless: bool = typer.Option(True, help="Run the browser headless. --no-headless to watch it."),
    max_steps: int = typer.Option(0, help="Override the step budget."),
) -> None:
    """Start a new run."""
    import dataclasses

    settings = SETTINGS
    overrides = {}
    if headless != settings.headless:
        overrides["headless"] = headless
    if max_steps:
        overrides["budget"] = dataclasses.replace(settings.budget, max_steps=max_steps)
    if overrides:
        settings = dataclasses.replace(settings, **overrides)

    operator = build_operator(settings)
    console.print(
        f"[dim]provider={operator.llm.name}/{operator.llm.model}  "
        f"tools={len(operator.registry)}  company documents={operator.documents_loaded}  "
        f"autonomy={operator.policy.autonomy}[/]"
    )
    operator.events.subscribe(_print_event)

    async def main() -> RunState:
        try:
            return await operator.kernel.start(goal)
        finally:
            await operator.aclose()

    state = asyncio.run(main())
    _summarise(state)
    raise typer.Exit(0 if state.status in {RunStatus.COMPLETED, RunStatus.AWAITING_HUMAN} else 1)


@app.command()
def resume(
    run_id: str = typer.Argument(..., help="Run to resume, or 'latest'."),
    response: str = typer.Option("", help="Your answer or the reason for your decision."),
    approve: bool = typer.Option(False, "--approve", help="Grant the approval."),
    reject: bool = typer.Option(False, "--reject", help="Refuse the approval."),
) -> None:
    """Answer a pending question or approval and continue the run."""
    operator = build_operator()
    operator.events.subscribe(_print_event)

    if run_id == "latest":
        latest = operator.events.latest_run_id()
        if latest is None:
            console.print("[red]no runs found[/]")
            raise typer.Exit(1)
        run_id = latest

    approved = True if approve else (False if reject else None)
    text = response or ("approved" if approve else "rejected" if reject else "")

    async def main() -> RunState:
        try:
            return await operator.kernel.resume(run_id, response=text, approved=approved)
        finally:
            await operator.aclose()

    state = asyncio.run(main())
    _summarise(state)


@app.command(name="runs")
def list_runs(limit: int = typer.Option(20)) -> None:
    """List recent runs."""
    operator = build_operator(reindex=False, require_llm=False)
    table = Table("run id", "status", "goal", title="runs")
    for row in operator.events.list_runs(limit):
        table.add_row(row["run_id"], row["status"], (row["goal"] or "")[:70])
    console.print(table)
    asyncio.run(operator.aclose())


@app.command()
def show(run_id: str = typer.Argument("latest")) -> None:
    """Print the report for a run."""
    operator = build_operator(reindex=False, require_llm=False)
    if run_id == "latest":
        run_id = operator.events.latest_run_id() or ""
    snapshot = operator.events.load_snapshot(run_id)
    if snapshot is None:
        console.print(f"[red]no run {run_id!r}[/]")
        raise typer.Exit(1)
    state = RunState.model_validate(snapshot)
    report = Path(SETTINGS.artifacts_dir) / run_id / "REPORT.md"
    if report.exists():
        console.print(report.read_text(encoding="utf-8"))
    else:
        from .evidence.bundle import render_report

        console.print(render_report(state))
    _summarise(state)
    asyncio.run(operator.aclose())


@app.command()
def tools() -> None:
    """Show the tool catalog exactly as the planner sees it."""
    operator = build_operator(reindex=False, require_llm=False)
    table = Table("tool", "surface", "risk", "reversible", "description", title="tool catalog")
    for entry in operator.registry.catalog():
        table.add_row(
            entry["name"],
            entry["surface"],
            entry["risk"],
            "yes" if entry["reversible"] else "no",
            entry["description"][:70].replace("\n", " "),
        )
    console.print(table)
    console.print(f"[dim]{len(operator.registry)} tools. Connector-defined tools are indistinguishable from built-ins.[/]")
    asyncio.run(operator.aclose())


@app.command()
def doctor() -> None:
    """Check configuration, provider reachability and the sandbox."""
    import httpx

    console.print(Panel("Configuration", border_style="cyan"))
    console.print(f"  provider        {SETTINGS.provider}/{SETTINGS.model}")
    console.print(f"  llm cache       {'on' if SETTINGS.llm_cache else 'off'} -> {SETTINGS.cassette_dir}")
    console.print(f"  company dir     {SETTINGS.company_dir}")
    console.print(f"  drive dir       {SETTINGS.drive_dir}")
    console.print(f"  erp / api       {SETTINGS.erp_url}  {SETTINGS.api_url}")

    ok = True
    try:
        operator = build_operator(require_llm=False)
        console.print(f"[green]  provider built  {operator.llm.name}/{operator.llm.model}[/]")
        console.print(f"[green]  tools           {len(operator.registry)} registered[/]")
        console.print(f"[green]  company memory  {operator.memory.count()} documents indexed[/]")
        console.print(f"[green]  policy          autonomy={operator.policy.autonomy}, gate={operator.policy.gate.value}[/]")
        asyncio.run(operator.aclose())
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        ok = False
        console.print(f"[red]  bootstrap failed: {exc}[/]")

    for label, url in (("Nexus ERP", f"{SETTINGS.erp_url}/_sandbox/health"), ("FinanceOps API", f"{SETTINGS.api_url}/health")):
        try:
            response = httpx.get(url, timeout=4.0)
            console.print(f"[green]  {label:<15} reachable (HTTP {response.status_code})[/]")
        except Exception:  # noqa: BLE001
            ok = False
            console.print(f"[red]  {label:<15} unreachable -- run 'python -m sandbox.serve'[/]")

    inbox = SETTINGS.drive_dir / "invoices-inbox"
    count = len(list(inbox.glob("*"))) if inbox.exists() else 0
    console.print(f"  drive inbox     {count} file(s)")

    try:
        from playwright.async_api import async_playwright  # noqa: F401

        console.print("[green]  playwright      importable[/]")
    except ImportError:
        ok = False
        console.print("[red]  playwright      missing -- 'python -m playwright install chromium'[/]")

    console.print(Panel("ready" if ok else "not ready", border_style="green" if ok else "red"))


@app.command()
def sandbox(
    reset: bool = typer.Option(False, "--reset", help="Reseed the sandbox world first."),
) -> None:
    """Print the sandbox company's current state, optionally reseeding first."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sandbox.store import STORE

    if reset:
        STORE.reset()
        console.print("[green]sandbox reset to seed state[/]")
    world = STORE.load()

    invoices = Table("invoice", "vendor", "PO", "status", "amount", "approved by", title="ERP invoice register")
    for invoice in world["invoices"]:
        invoices.add_row(
            invoice["number"],
            invoice["vendor"][:28],
            invoice["po_number"],
            invoice["status"],
            f"{int(invoice['amount']):,}",
            invoice.get("approved_by") or "-",
        )
    console.print(invoices)

    orders = Table("PO", "vendor", "ordered", "billed", "remaining", title="purchase orders")
    for po in world["purchase_orders"]:
        remaining = int(po["total"]) - int(po.get("billed", 0))
        orders.add_row(
            po["number"],
            po["vendor"][:28],
            f"{int(po['total']):,}",
            f"{int(po.get('billed', 0)):,}",
            f"{remaining:,}",
        )
    console.print(orders)

    ledger = Table("entry", "invoice", "amount", "GL code", title="general ledger")
    for entry in world.get("ledger", []):
        ledger.add_row(
            entry["entry_id"], entry["invoice_number"], f"{int(entry['amount']):,}", entry["gl_code"]
        )
    console.print(ledger)

    for folder in ("invoices-inbox", "invoices-processed", "invoices-exceptions"):
        path = SETTINGS.drive_dir / folder
        names = sorted(p.name for p in path.glob("*")) if path.exists() else []
        console.print(f"  [bold]{folder}[/]: {', '.join(names) or '(empty)'}")


@app.command()
def serve(port: int = typer.Option(8780)) -> None:
    """Run the live dashboard."""
    import uvicorn

    from .web.app import create_app

    console.print(f"[bold]dashboard[/] http://127.0.0.1:{port}")
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    app()
