"""The second task family, end to end, through the real kernel.

This is the generalisation claim under test. Nothing in the kernel, the tool
layer, the policy engine or the verifier is specific to onboarding -- the task
exists as an SOP, a read-only YAML connector, and a desktop application. If the
architecture only *looked* general, this file would not run.

As in ``test_full_workflow.py``, only the model's reasoning is scripted.
Everything else is real: a prose memo parsed out of a PDF, a Qt window driven
through UI Automation, HTTP reads, the policy engine, a genuine
human-in-the-loop pause, verification probes and an evidence bundle. Every final
assertion reads the **system of record**, not the operator's account of it.

Note the contrast with the finance family. There, the human-in-the-loop pause is
*policy-driven*: the operator is stopped before it attempts something beyond its
authority. Here it is *refusal-driven*: the application itself refuses to grant a
restricted group, and the operator escalates in response. Two different triggers,
one mechanism, no special-casing in the kernel.
"""

from __future__ import annotations

import dataclasses
import sys

import httpx
import pytest

from centralign.config import SETTINGS, Budget
from centralign.events.log import EventLog
from centralign.llm.replay import ScriptedProvider
from centralign.memory.store import MemoryStore
from centralign.policy.guard import PolicyGuard
from centralign.runtime.kernel import Kernel, build_registry
from centralign.runtime.state import RunStatus, StepStatus

from .conftest import intake, plan, probes, report, step, verdict


def _ready() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import PySide6  # noqa: F401
        import pywinauto  # noqa: F401
    except ImportError:
        return False
    try:
        httpx.get(f"{SETTINGS.api_url}/health", timeout=2.0)
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _ready(), reason="needs Windows, PySide6/pywinauto, and the sandbox running"
)

JOINER = "p.venkatesan"
MEMO = "hr-inbox/new-joiner-priya-venkatesan.pdf"

CRITERIA = [
    ("ac1", f"An account for {JOINER} exists with the role and department from the request"),
    ("ac2", f"The standard QA Engineer hardware kit is assigned to {JOINER}"),
    ("ac3", f"{JOINER} is a member of every unrestricted access group for the role"),
    ("ac4", "Any restricted group that was requested has an approval or a recorded escalation"),
    ("ac5", "The joiner request has been filed out of the HR inbox"),
]


def _discover_steps() -> list[dict]:
    return [
        step("company_memory_search", id="s1", intent="find the new joiner procedure",
             args={"query": "new joiner setup onboarding SOP standard kit access groups"}),
        step("drive_list", id="s2", intent="see what is waiting in the HR inbox",
             args={"folder": "hr-inbox"}),
        step("drive_read", id="s3", intent="read the joiner request memo",
             args={"path": MEMO}),
        step("account_lookup", id="s4", intent="check the directory for an existing account",
             args={}),
        step("asset_lookup", id="s5", intent="find hardware that is actually available",
             args={"status": "available"}),
    ]


def _account_steps() -> list[dict]:
    return [
        step("desktop_open", id="s6", intent="open the Asset and Access Manager", args={}),
        step("desktop_click", id="s7", intent="go to the Directory section",
             args={"target": "Directory"}),
        step("desktop_fill", id="s8", intent="enter the joiner's details", args={
            "fields": {
                "Full name": "Priya Venkatesan",
                "Username": JOINER,
                "Role": "QA Engineer",
                "Department": "Engineering",
                "Start date": "2026-10-06",
            }
        }),
        step("desktop_click", id="s9", intent="create the account",
             args={"target": "Create account"}),
    ]


def _hardware_steps() -> list[dict]:
    return [
        step("desktop_click", id="s10", intent="go to the Assets section",
             args={"target": "Assets"}),
        step("desktop_fill", id="s11", intent="select the laptop for the joiner",
             args={"fields": {"Asset tag": "LAP-0141", "Assign to username": JOINER}}),
        step("desktop_click", id="s12", intent="issue the laptop",
             args={"target": "Assign asset"}),
        step("desktop_fill", id="s13", intent="select the monitor for the joiner",
             args={"fields": {"Asset tag": "MON-0088", "Assign to username": JOINER}}),
        step("desktop_click", id="s14", intent="issue the monitor",
             args={"target": "Assign asset"}),
    ]


def _access_steps() -> list[dict]:
    steps = [
        step("desktop_click", id="s15", intent="go to the Access Groups section",
             args={"target": "Access Groups"}),
    ]
    for index, group in enumerate(("eng-general", "qa-automation", "vpn-standard"), start=16):
        steps += [
            step("desktop_fill", id=f"s{index}a", intent=f"select the {group} group",
                 args={"fields": {"Grant to username": JOINER, "Access group": group}}),
            step("desktop_click", id=f"s{index}b", intent=f"grant {group}",
                 args={"target": "Grant access"}),
        ]
    # The restricted one: the application refuses it outright.
    steps += [
        step("desktop_fill", id="s19a", intent="attempt the requested finance-systems access",
             args={"fields": {"Grant to username": JOINER, "Access group": "finance-systems"}}),
        step("desktop_click", id="s19b", intent="attempt to grant finance-systems",
             args={"target": "Grant access"}),
    ]
    return steps


def _closeout_steps() -> list[dict]:
    return [
        step("escalate_to_human", id="s20",
             intent="refer the restricted group to a person", args={
                 "subject": f"finance-systems access for {JOINER}",
                 "findings": (
                     "The joiner memo asks for finance systems access for ERP invoice test "
                     "data. The Asset and Access Manager refuses to grant finance-systems "
                     "from the Access Groups screen because it is restricted and needs "
                     "Finance Controller approval. Everything else in the standard QA "
                     "Engineer kit has been provisioned."
                 ),
                 "recommendation": (
                     "Approve time-limited finance-systems access for the second week, or "
                     "decline and I will record it as not provided."
                 ),
                 "options": ["approve", "decline"],
             }),
        step("record_result", id="s21", intent="record the provisioning outcome", args={
            "item": f"{JOINER} - account, hardware and standard access",
            "status": "completed",
            "detail": (
                "Account created as QA Engineer in Engineering. LAP-0141 and MON-0088 "
                "issued. eng-general, qa-automation and vpn-standard granted."
            ),
            "evidence": ["Asset and Access Manager", "itops directory"],
        }),
        step("record_result", id="s22", intent="record the escalated item", args={
            "item": f"{JOINER} - finance-systems access",
            "status": "escalated",
            "detail": "Restricted group; refused by the application and referred for approval.",
            "evidence": ["Access Groups screen refusal"],
        }),
        step("drive_move", id="s23", intent="file the joiner request",
             args={"path": MEMO, "to_folder": "hr-completed"}),
    ]


VERIFY_PROBES = probes(
    {"id": "p1", "criterion_id": "ac1", "tool": "account_lookup", "args": {"username": JOINER},
     "looking_for": "an account with role QA Engineer in Engineering"},
    {"id": "p2", "criterion_id": "ac2", "tool": "asset_lookup", "args": {"assigned_to": JOINER},
     "looking_for": "a laptop and a monitor assigned to the joiner"},
    {"id": "p3", "criterion_id": "ac3", "tool": "access_group_lookup", "args": {"member": JOINER},
     "looking_for": "membership of the unrestricted groups, and NOT finance-systems"},
    {"id": "p4", "criterion_id": "ac5", "tool": "drive_list", "args": {"folder": "hr-inbox"},
     "looking_for": "the inbox is clear"},
)


@pytest.fixture
async def operator(tmp_path):
    httpx.post(f"{SETTINGS.erp_url}/_sandbox/reset", timeout=15.0)

    settings = dataclasses.replace(
        SETTINGS,
        var_dir=tmp_path / "var",
        artifacts_dir=tmp_path / "artifacts",
        llm_cache=False,
        budget=Budget(max_steps=90, max_llm_calls=40, wall_clock_seconds=900),
    )
    settings.ensure_dirs()

    memory = MemoryStore(settings.memory_db)
    memory.ingest_company_dir(settings.company_dir)
    events = EventLog(settings.event_db)

    def build(responses):
        return Kernel(
            settings=settings,
            llm=ScriptedProvider(list(responses)),
            registry=build_registry(settings),
            memory=memory,
            policy=PolicyGuard.from_file(settings.company_dir / "policies.yaml"),
            events=events,
        )

    yield build, settings, events
    memory.close()
    events.close()


def _account(username: str) -> dict | None:
    body = httpx.get(f"{SETTINGS.api_url}/itops/accounts", timeout=15.0).json()
    return next((a for a in body["accounts"] if a["username"] == username), None)


def _assets_of(username: str) -> list[dict]:
    return httpx.get(
        f"{SETTINGS.api_url}/itops/assets", params={"assigned_to": username}, timeout=15.0
    ).json()["assets"]


def _groups_of(username: str) -> set[str]:
    body = httpx.get(
        f"{SETTINGS.api_url}/itops/access-groups", params={"member": username}, timeout=15.0
    ).json()
    return set(body["access_groups"])


class TestOnboardingEndToEnd:
    async def test_the_joiner_is_actually_provisioned(self, operator):
        build, settings, events = operator
        kernel = build(
            [
                intake("Set up the new joiner from the HR inbox request", CRITERIA),
                plan(
                    *_discover_steps(),
                    *_account_steps(),
                    *_hardware_steps(),
                    *_access_steps(),
                    *_closeout_steps(),
                ),
                VERIFY_PROBES,
                verdict(
                    ("ac1", "met"), ("ac2", "met"), ("ac3", "met"),
                    ("ac4", "met"), ("ac5", "met"), complete=True,
                ),
                report("Priya Venkatesan provisioned; finance-systems access awaiting approval."),
            ]
        )

        state = await kernel.start(
            "Someone new is joining - there's a setup request in the HR inbox. "
            "Please get them set up."
        )

        # --- the application refused the restricted group, so the run paused ---
        assert state.status is RunStatus.AWAITING_HUMAN, state.error
        pending = state.pending_human()
        assert pending is not None
        assert "finance-systems" in pending.prompt
        assert "Finance Controller approval" in pending.prompt or "restricted" in pending.prompt.lower()

        # --- everything that did not need a person is already done -------------
        account = _account(JOINER)
        assert account is not None, "the account was never created"
        assert account["role"] == "QA Engineer"
        assert account["department"] == "Engineering"
        assert account["start_date"] == "2026-10-06"

        assert _groups_of(JOINER) >= {"eng-general", "qa-automation", "vpn-standard"}
        assert "finance-systems" not in _groups_of(JOINER), "a restricted group must not be granted"

        # --- a person answers, and the run finishes ---------------------------
        resumed = await kernel.resume(
            state.run_id,
            response="Approved for the second week only. I will grant it myself on Monday.",
            approved=True,
        )
        assert resumed.status is RunStatus.COMPLETED, resumed.error

        kit = {a["tag"] for a in _assets_of(JOINER)}
        assert kit == {"LAP-0141", "MON-0088"}, f"wrong hardware kit: {kit}"
        assert all(a["status"] == "assigned" for a in _assets_of(JOINER))

        # Hardware belonging to others must be untouched.
        assert _assets_of("r.iyer")[0]["tag"] == "LAP-0142"

        filed = {p.name for p in (settings.drive_dir / "hr-completed").glob("*")}
        assert "new-joiner-priya-venkatesan.pdf" in filed
        assert not list((settings.drive_dir / "hr-inbox").glob("*.pdf"))

        failed = [s.id for s in resumed.plan.steps if s.status is StepStatus.FAILED]
        assert not failed, f"steps failed: {failed}"

        # --- the evidence bundle shows desktop work, not just HTTP ------------
        bundle = settings.artifacts_dir / state.run_id
        text = (bundle / "REPORT.md").read_text(encoding="utf-8")
        assert JOINER in text
        assert "finance-systems" in text
        assert "escalated" in text
        shots = list(bundle.glob("desk-*.png"))
        assert len(shots) >= 8, f"expected screenshots of the desktop work, got {len(shots)}"
        assert all(s.stat().st_size > 1000 for s in shots)

        evidence = (bundle / "verification.md").read_text(encoding="utf-8")
        assert "QA Engineer" in evidence, "the verifier must have re-read the directory"

    async def test_a_duplicate_joiner_is_caught_rather_than_duplicated(self, operator):
        """Running the same setup twice must not create a second account."""
        build, settings, events = operator
        existing = httpx.get(f"{SETTINGS.api_url}/itops/accounts", timeout=15.0).json()["count"]

        kernel = build(
            [
                intake("Set up Rohan Iyer", [("ac1", "An account for r.iyer exists exactly once")]),
                plan(
                    step("desktop_open", id="s1", intent="open the app", args={}),
                    step("desktop_click", id="s2", intent="go to Directory", args={"target": "Directory"}),
                    step("desktop_fill", id="s3", intent="enter details", args={
                        "fields": {"Full name": "Rohan Iyer", "Username": "r.iyer"}
                    }),
                    step("desktop_click", id="s4", intent="attempt to create", args={"target": "Create account"}),
                    step("record_result", id="s5", intent="record the outcome", args={
                        "item": "r.iyer", "status": "skipped",
                        "detail": "Account already exists; no second account created.",
                    }),
                ),
                probes({"tool": "account_lookup", "criterion_id": "ac1", "args": {"username": "r.iyer"}}),
                verdict(("ac1", "met"), complete=True),
                report("Account already existed; nothing created."),
            ]
        )
        state = await kernel.start("set up Rohan Iyer")

        after = httpx.get(f"{SETTINGS.api_url}/itops/accounts", timeout=15.0).json()["count"]
        assert after == existing, "a duplicate account must not be created"
        assert state.status is RunStatus.COMPLETED

        refusal = next(s for s in state.plan.steps if s.id == "s4")
        assert refusal.observation.data["refused"] is True
        assert "already exists" in refusal.observation.data["status"]

    async def test_provisioning_is_allowed_without_approval_but_restricted_access_is_not(
        self, operator
    ):
        """The policy split that makes the run autonomous but still safe."""
        build, settings, _ = operator
        kernel = build([])
        guard = kernel.policy
        click = kernel.registry.get("desktop_click")

        routine = guard.assess(click, {"target": "Create account"})
        assert routine.allowed is True
        assert routine.requires_approval is False, routine.reason
        assert "IT-001-routine-provisioning" in routine.matched_rules

        # Navigation is not even a write.
        assert guard.assess(click, {"target": "Assets"}).requires_approval is False
