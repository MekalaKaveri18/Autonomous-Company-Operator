"""Desktop surface tests against the real Qt application via UI Automation.

These drive an actual window: launching it, switching tabs, typing into fields,
clicking buttons, and reading refusals back off the status line. They are the
evidence that the desktop surface is real computer use rather than a shim over
the shared JSON file -- every assertion goes through UIA, and the state checks
read the application's own read-only API afterwards.

Skipped off Windows, and skipped if the sandbox API is not running.
"""

from __future__ import annotations

import sys

import httpx
import pytest

from centralign.config import SETTINGS
from centralign.events.log import EventLog
from centralign.memory.store import MemoryStore
from centralign.runtime.state import RiskTier, RunState
from centralign.tools.base import FailureKind, ToolContext
from centralign.tools.connector import load_connectors
from centralign.tools.registry import ToolRegistry


def _api_up() -> bool:
    try:
        httpx.get(f"{SETTINGS.api_url}/health", timeout=2.0)
        return True
    except Exception:  # noqa: BLE001
        return False


def _desktop_importable() -> bool:
    try:
        import PySide6  # noqa: F401
        import pywinauto  # noqa: F401

        return True
    except ImportError:
        return False


pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="UI Automation is Windows-only"),
    pytest.mark.skipif(not _desktop_importable(), reason="PySide6/pywinauto not installed"),
    pytest.mark.skipif(not _api_up(), reason="sandbox not running"),
]


@pytest.fixture
def registry() -> ToolRegistry:
    from centralign.tools.desktop_tools import DESKTOP_TOOLS

    reg = ToolRegistry(list(DESKTOP_TOOLS))
    for tool in load_connectors(SETTINGS.company_dir / "connectors"):
        if tool.service == "itops":
            reg.register(tool)
    return reg


@pytest.fixture
async def ctx(tmp_path):
    """Fresh sandbox state, throwaway local state, desktop session torn down."""
    httpx.post(f"{SETTINGS.erp_url}/_sandbox/reset", timeout=15.0)

    events = EventLog(tmp_path / "events.sqlite3")
    memory = MemoryStore(tmp_path / "memory.sqlite3")
    context = ToolContext(
        settings=SETTINGS,
        run_id="desktop-test",
        events=events,
        state=RunState(run_id="desktop-test", goal="desktop integration test"),
        memory=memory,
        artifacts_dir=tmp_path / "artifacts",
    )
    try:
        yield context
    finally:
        session = context.resources.pop("desktop", None)
        if session is not None:
            await session.close()
        client = context.resources.pop("http_client", None)
        if client is not None:
            await client.aclose()
        events.close()
        memory.close()


def _accounts() -> list[dict]:
    return httpx.get(f"{SETTINGS.api_url}/itops/accounts", timeout=15.0).json()["accounts"]


def _assets() -> list[dict]:
    return httpx.get(f"{SETTINGS.api_url}/itops/assets", timeout=15.0).json()["assets"]


def _groups() -> dict:
    return httpx.get(f"{SETTINGS.api_url}/itops/access-groups", timeout=15.0).json()["access_groups"]


async def _open(registry, ctx, tab: str = "Directory"):
    """Open the app and move to a known tab.

    The window keeps whichever tab was last opened, across runs and across tests.
    That is realistic -- and it is why the window summary reports which tab is
    showing -- but a test that assumed a tab would be flaky, so navigate.
    """
    observation = await registry.get("desktop_open").invoke({}, ctx)
    assert observation.ok, observation.error
    if tab not in observation.data.get("selected_tab", ""):
        switched = await registry.get("desktop_click").invoke({"target": tab}, ctx)
        assert switched.ok, switched.error
        return switched
    return observation


async def _click(registry, ctx, target: str):
    return await registry.get("desktop_click").invoke({"target": target}, ctx)


async def _fill(registry, ctx, fields: dict):
    return await registry.get("desktop_fill").invoke({"fields": fields}, ctx)


class TestObservation:
    async def test_it_launches_or_attaches_and_reads_the_window(self, registry, ctx):
        observation = await _open(registry, ctx)
        assert observation.ok, observation.error
        window = observation.data["window"]
        assert "Asset and Access Manager" in window
        assert "TABS:" in window
        # Existing directory rows must be readable, or the operator is blind.
        assert "r.iyer" in window and "QA Engineer" in window
        assert observation.artifacts, "every desktop action should leave a screenshot"

    async def test_input_fields_are_reported_by_name(self, registry, ctx):
        observation = await _open(registry, ctx)
        window = observation.data["window"]
        for field in ("Full name", "Username", "Role", "Department", "Start date"):
            assert f"name={field!r}" in window, f"{field} not offered to the planner"
        assert "Create account" in window

    async def test_dropdown_options_are_offered(self, registry, ctx):
        observation = await _open(registry, ctx, "Directory")
        window = observation.data["window"]
        # Must come from the Role/Department dropdowns, not from list rows that
        # happen to contain the same words.
        assert "name='Role'" in window and "options=" in window
        role_line = next(line for line in window.splitlines() if "name='Role'" in line)
        assert "QA Engineer" in role_line, role_line

    async def test_acting_before_opening_is_a_precondition_failure(self, registry, ctx):
        observation = await registry.get("desktop_read").invoke({}, ctx)
        assert not observation.ok
        assert observation.error_kind == FailureKind.PRECONDITION
        assert "desktop_open" in (observation.error or "")

    async def test_switching_tabs_reveals_that_tabs_controls(self, registry, ctx):
        await _open(registry, ctx)
        assets = await _click(registry, ctx, "Assets")
        assert assets.ok
        window = assets.data["window"]
        assert "name='Asset tag'" in window
        assert "LAP-0141" in window and "available" in window
        # Hardware already held by someone else must be visible as such.
        assert "r.iyer" in window

        access = await _click(registry, ctx, "Access Groups")
        assert "name='Grant to username'" in access.data["window"]
        assert "finance-systems" in access.data["window"]
        assert "restricted" in access.data["window"]


class TestRiskClassification:
    def test_navigation_is_not_treated_as_a_mutation(self, registry):
        tool = registry.get("desktop_click")
        assert tool.risk_for({"target": "Assets"}) is RiskTier.READ

    def test_creating_and_granting_are_sensitive(self, registry):
        tool = registry.get("desktop_click")
        for target in ("Create account", "Assign asset", "Grant access"):
            assert tool.risk_for({"target": target}) is RiskTier.SENSITIVE, target


class TestAccountCreation:
    async def test_it_creates_an_account_that_the_api_then_confirms(self, registry, ctx):
        await _open(registry, ctx)
        filled = await _fill(
            registry,
            ctx,
            {
                "Full name": "Priya Venkatesan",
                "Username": "p.venkatesan",
                "Role": "QA Engineer",
                "Department": "Engineering",
                "Start date": "2026-10-06",
            },
        )
        assert filled.ok, filled.error

        clicked = await _click(registry, ctx, "Create account")
        assert clicked.ok
        assert clicked.data["refused"] is False, clicked.data["status"]
        assert "created" in clicked.data["status"].lower()

        # The real check: the system of record, read through a different channel.
        created = next((a for a in _accounts() if a["username"] == "p.venkatesan"), None)
        assert created is not None, "the account was reported created but does not exist"
        assert created["full_name"] == "Priya Venkatesan"
        assert created["role"] == "QA Engineer"
        assert created["department"] == "Engineering"

    async def test_a_duplicate_account_is_refused_and_names_the_holder(self, registry, ctx):
        await _open(registry, ctx)
        await _fill(registry, ctx, {"Full name": "Rohan Iyer Two", "Username": "r.iyer"})
        clicked = await _click(registry, ctx, "Create account")

        assert clicked.ok, "a refusal is a successful observation of a 'no'"
        assert clicked.data["refused"] is True
        status = clicked.data["status"]
        assert "already exists" in status
        assert "Rohan Iyer" in status
        assert len([a for a in _accounts() if a["username"] == "r.iyer"]) == 1

    async def test_a_missing_required_field_is_refused(self, registry, ctx):
        before = len(_accounts())
        await _open(registry, ctx, "Directory")
        # The form keeps whatever was typed into it last, so clear Full name
        # explicitly rather than assuming the field starts empty.
        await _fill(registry, ctx, {"Full name": "", "Username": "x.nobody"})
        clicked = await _click(registry, ctx, "Create account")
        assert clicked.data["refused"] is True
        assert "required" in clicked.data["status"].lower()
        assert len(_accounts()) == before

    async def test_an_unknown_field_name_lists_what_is_available(self, registry, ctx):
        await _open(registry, ctx, "Directory")
        observation = await _fill(registry, ctx, {"Employee Serial Number": "7"})
        assert not observation.ok
        assert observation.error_kind == FailureKind.NOT_FOUND
        assert "available here --" in (observation.error or "")

    async def test_an_invalid_dropdown_value_fails_usefully(self, registry, ctx):
        await _open(registry, ctx, "Directory")
        observation = await _fill(registry, ctx, {"Role": "Supreme Overlord"})
        assert not observation.ok
        assert "not one of the options" in (observation.error or "")


class TestAssetAssignment:
    async def test_it_assigns_an_available_asset(self, registry, ctx):
        await _open(registry, ctx, "Directory")
        await _fill(registry, ctx, {"Full name": "Priya Venkatesan", "Username": "p.venkatesan"})
        await _click(registry, ctx, "Create account")

        await _click(registry, ctx, "Assets")
        await _fill(registry, ctx, {"Asset tag": "LAP-0141", "Assign to username": "p.venkatesan"})
        clicked = await _click(registry, ctx, "Assign asset")

        assert clicked.data["refused"] is False, clicked.data["status"]
        asset = next(a for a in _assets() if a["tag"] == "LAP-0141")
        assert asset["status"] == "assigned"
        assert asset["assigned_to"] == "p.venkatesan"

    async def test_an_asset_held_by_someone_else_is_refused_and_names_them(self, registry, ctx):
        await _open(registry, ctx, "Directory")
        await _fill(registry, ctx, {"Full name": "Priya Venkatesan", "Username": "p.venkatesan"})
        await _click(registry, ctx, "Create account")

        await _click(registry, ctx, "Assets")
        await _fill(registry, ctx, {"Asset tag": "LAP-0142", "Assign to username": "p.venkatesan"})
        clicked = await _click(registry, ctx, "Assign asset")

        assert clicked.data["refused"] is True
        assert "already assigned to r.iyer" in clicked.data["status"]
        assert next(a for a in _assets() if a["tag"] == "LAP-0142")["assigned_to"] == "r.iyer"

    async def test_an_asset_in_repair_cannot_be_assigned(self, registry, ctx):
        await _open(registry, ctx, "Assets")
        await _fill(registry, ctx, {"Asset tag": "LAP-0143", "Assign to username": "r.iyer"})
        clicked = await _click(registry, ctx, "Assign asset")

        assert clicked.data["refused"] is True
        assert "in repair" in clicked.data["status"]

    async def test_assigning_to_a_nonexistent_account_is_refused(self, registry, ctx):
        await _open(registry, ctx, "Assets")
        await _fill(registry, ctx, {"Asset tag": "MON-0088", "Assign to username": "nobody.here"})
        clicked = await _click(registry, ctx, "Assign asset")

        assert clicked.data["refused"] is True
        assert "No account" in clicked.data["status"]
        assert next(a for a in _assets() if a["tag"] == "MON-0088")["assigned_to"] == ""

    async def test_an_unknown_tag_lists_what_is_available(self, registry, ctx):
        await _open(registry, ctx, "Assets")
        await _fill(registry, ctx, {"Asset tag": "LAP-9999", "Assign to username": "r.iyer"})
        clicked = await _click(registry, ctx, "Assign asset")

        assert clicked.data["refused"] is True
        assert "Available now:" in clicked.data["status"]


class TestAccessGroups:
    async def test_it_grants_an_unrestricted_group(self, registry, ctx):
        await _open(registry, ctx, "Directory")
        await _fill(registry, ctx, {"Full name": "Priya Venkatesan", "Username": "p.venkatesan"})
        await _click(registry, ctx, "Create account")

        await _click(registry, ctx, "Access Groups")
        await _fill(registry, ctx, {"Grant to username": "p.venkatesan", "Access group": "qa-automation"})
        clicked = await _click(registry, ctx, "Grant access")

        assert clicked.data["refused"] is False, clicked.data["status"]
        assert "p.venkatesan" in _groups()["qa-automation"]["members"]

    async def test_a_restricted_group_is_refused_and_demands_approval(self, registry, ctx):
        """The refusal that forces a human into the loop."""
        await _open(registry, ctx, "Directory")
        await _fill(registry, ctx, {"Full name": "Priya Venkatesan", "Username": "p.venkatesan"})
        await _click(registry, ctx, "Create account")

        await _click(registry, ctx, "Access Groups")
        await _fill(
            registry, ctx, {"Grant to username": "p.venkatesan", "Access group": "finance-systems"}
        )
        clicked = await _click(registry, ctx, "Grant access")

        assert clicked.data["refused"] is True
        status = clicked.data["status"]
        assert "restricted" in status
        assert "Finance Controller approval" in status
        assert "p.venkatesan" not in _groups()["finance-systems"]["members"]

    async def test_granting_a_group_twice_is_reported_as_no_change(self, registry, ctx):
        await _open(registry, ctx, "Access Groups")
        await _fill(registry, ctx, {"Grant to username": "r.iyer", "Access group": "eng-general"})
        clicked = await _click(registry, ctx, "Grant access")

        # Idempotent in effect: already a member, so success with no change.
        assert clicked.data["refused"] is False
        assert "already a member" in clicked.data["status"]
        assert _groups()["eng-general"]["members"].count("r.iyer") == 1


class TestReadOnlyConnector:
    async def test_the_itops_connector_exposes_no_write_operations(self, registry):
        """The write gap is the point: it forces the operator to use the GUI."""
        names = set(registry.names)
        assert {"account_lookup", "asset_lookup", "access_group_lookup"} <= names
        assert not any("create" in n or "assign" in n or "grant" in n for n in names if "lookup" not in n and not n.startswith("desktop_"))

    async def test_every_itops_operation_is_read_only_so_the_verifier_may_use_it(self, registry):
        for name in ("account_lookup", "asset_lookup", "access_group_lookup"):
            assert registry.get(name).risk is RiskTier.READ

    async def test_a_missing_account_maps_to_not_found(self, registry, ctx):
        observation = await registry.get("account_lookup").invoke({"username": "ghost.user"}, ctx)
        assert not observation.ok
        assert observation.error_kind == FailureKind.NOT_FOUND
        assert not observation.retryable
