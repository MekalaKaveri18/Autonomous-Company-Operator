"""Policy evaluation and blackboard reference resolution.

Both are places where a subtle bug is silent and expensive: a policy rule that
quietly fails to match removes an approval gate, and a reference that resolves to
the wrong value puts the wrong number on an invoice. Neither failure announces
itself at run time, so they are pinned down here.
"""

from __future__ import annotations

import pytest
import yaml

from centralign.config import SETTINGS
from centralign.policy.guard import PolicyGuard
from centralign.runtime.state import (
    AcceptanceCriterion,
    Observation,
    Plan,
    RiskTier,
    RunState,
    Step,
    StepStatus,
    UnresolvedReference,
    build_ref_context,
    lookup_path,
    resolve_refs,
)
from centralign.tools.base import Tool


class _Tool(Tool):
    """Minimal tool for policy assertions."""

    args_schema = {"type": "object", "properties": {}, "additionalProperties": True}

    def __init__(self, name: str, risk: RiskTier, *, surface: str = "test", reversible: bool = True):
        self.name = name
        self.risk = risk
        self.surface = surface
        self.reversible = reversible
        self.description = name

    async def execute(self, args, ctx):  # pragma: no cover - never invoked here
        return Observation(ok=True)


class TestAutonomyGates:
    @pytest.mark.parametrize(
        "autonomy,risk,expect_approval",
        [
            ("supervised", RiskTier.READ, False),
            ("supervised", RiskTier.WRITE, True),
            ("supervised", RiskTier.SENSITIVE, True),
            ("standard", RiskTier.READ, False),
            ("standard", RiskTier.WRITE, False),
            ("standard", RiskTier.SENSITIVE, True),
            ("standard", RiskTier.EXTERNAL, True),
            ("high", RiskTier.SENSITIVE, False),
            ("high", RiskTier.EXTERNAL, True),
        ],
    )
    def test_gate_depends_on_autonomy_and_risk(self, autonomy, risk, expect_approval):
        guard = PolicyGuard({"autonomy": autonomy})
        decision = guard.assess(_Tool("t", risk), {})
        assert decision.allowed is True
        assert decision.requires_approval is expect_approval

    def test_an_unknown_autonomy_level_falls_back_to_standard(self):
        guard = PolicyGuard({"autonomy": "cowboy"})
        assert guard.gate is RiskTier.SENSITIVE

    def test_a_denied_tool_is_not_allowed_at_all(self):
        guard = PolicyGuard({"autonomy": "high", "denied_tools": ["dangerous"]})
        decision = guard.assess(_Tool("dangerous", RiskTier.READ), {})
        assert decision.allowed is False
        assert decision.requires_approval is False


class TestRuleMatching:
    def test_a_numeric_threshold_escalates_only_above_the_limit(self):
        guard = PolicyGuard(
            {
                "autonomy": "high",
                "rules": [
                    {
                        "id": "big-money",
                        "match": {"tools": ["pay"], "arg": {"name": "amount", "gte": 50000}},
                        "effect": {"require_approval": True, "reason": "over the limit"},
                    }
                ],
            }
        )
        tool = _Tool("pay", RiskTier.WRITE)
        assert guard.assess(tool, {"amount": 49999}).requires_approval is False
        assert guard.assess(tool, {"amount": 50000}).requires_approval is True
        assert "over the limit" in guard.assess(tool, {"amount": 60000}).reason

    def test_a_non_numeric_argument_does_not_match_a_threshold(self):
        guard = PolicyGuard(
            {
                "autonomy": "high",
                "rules": [
                    {
                        "id": "big-money",
                        "match": {"arg": {"name": "amount", "gte": 10}},
                        "effect": {"require_approval": True},
                    }
                ],
            }
        )
        assert guard.assess(_Tool("t", RiskTier.WRITE), {"amount": "not a number"}).requires_approval is False

    def test_an_allowlist_rule_fires_for_values_outside_the_list(self):
        guard = PolicyGuard(
            {
                "autonomy": "high",
                "rules": [
                    {
                        "id": "vendor-check",
                        "match": {"arg_not_in": {"name": "vendor", "allow_from": "vendors.approved"}},
                        "effect": {"deny": True, "reason": "unapproved vendor"},
                    }
                ],
                "data": {"vendors": {"approved": ["Helios Logistics"]}},
            }
        )
        tool = _Tool("post", RiskTier.WRITE)
        assert guard.assess(tool, {"vendor": "Helios Logistics"}).allowed is True
        denied = guard.assess(tool, {"vendor": "Zenith Office Supplies"})
        assert denied.allowed is False
        assert "unapproved vendor" in denied.reason

    def test_allowlist_matching_ignores_case_and_surrounding_space(self):
        guard = PolicyGuard(
            {
                "autonomy": "high",
                "rules": [
                    {
                        "id": "v",
                        "match": {"arg_not_in": {"name": "vendor", "allow_from": "vendors.approved"}},
                        "effect": {"deny": True},
                    }
                ],
                "data": {"vendors": {"approved": ["Helios Logistics"]}},
            }
        )
        assert guard.assess(_Tool("t", RiskTier.WRITE), {"vendor": "  helios logistics "}).allowed is True

    def test_all_conditions_in_a_match_must_hold(self):
        guard = PolicyGuard(
            {
                "autonomy": "high",
                "rules": [
                    {
                        "id": "both",
                        "match": {"tools": ["pay"], "arg": {"name": "amount", "gte": 10}},
                        "effect": {"require_approval": True},
                    }
                ],
            }
        )
        # Right tool, amount below threshold -> no match.
        assert guard.assess(_Tool("pay", RiskTier.WRITE), {"amount": 5}).requires_approval is False
        # Wrong tool, amount above threshold -> no match.
        assert guard.assess(_Tool("read", RiskTier.WRITE), {"amount": 500}).requires_approval is False

    def test_an_empty_match_never_fires(self):
        guard = PolicyGuard(
            {"autonomy": "high", "rules": [{"id": "x", "match": {}, "effect": {"deny": True}}]}
        )
        assert guard.assess(_Tool("t", RiskTier.READ), {}).allowed is True

    def test_an_irreversible_rule_targets_only_irreversible_tools(self):
        guard = PolicyGuard(
            {
                "autonomy": "high",
                "rules": [
                    {
                        "id": "no-undo",
                        "match": {"irreversible": True},
                        "effect": {"require_approval": True, "reason": "cannot be undone"},
                    }
                ],
            }
        )
        assert guard.assess(_Tool("a", RiskTier.WRITE, reversible=True), {}).requires_approval is False
        assert guard.assess(_Tool("b", RiskTier.WRITE, reversible=False), {}).requires_approval is True

    def test_a_rule_can_escalate_the_risk_tier(self):
        guard = PolicyGuard(
            {
                "autonomy": "standard",
                "rules": [
                    {
                        "id": "escalate",
                        "match": {"tools": ["t"]},
                        "effect": {"risk": "sensitive", "reason": "treated as sensitive"},
                    }
                ],
            }
        )
        decision = guard.assess(_Tool("t", RiskTier.WRITE), {})
        assert decision.risk is RiskTier.SENSITIVE
        assert decision.requires_approval is True


class TestFailClosed:
    def test_an_unreadable_policy_file_drops_to_supervised(self, tmp_path):
        bad = tmp_path / "policies.yaml"
        bad.write_text("autonomy: [unclosed", encoding="utf-8")
        guard = PolicyGuard.from_file(bad)
        assert guard.autonomy == "supervised"
        assert guard.gate is RiskTier.WRITE

    def test_a_missing_policy_file_uses_safe_defaults(self, tmp_path):
        guard = PolicyGuard.from_file(tmp_path / "absent.yaml")
        assert guard.gate is RiskTier.SENSITIVE

    def test_an_unevaluable_rule_escalates_rather_than_being_ignored(self):
        guard = PolicyGuard(
            {
                "autonomy": "high",
                # allow_from points at a scalar, so the condition cannot be evaluated.
                "rules": [
                    {
                        "id": "broken",
                        "match": {"arg_not_in": {"name": "v", "allow_from": "nope.nothing"}},
                        "effect": {"deny": True},
                    }
                ],
            }
        )
        decision = guard.assess(_Tool("t", RiskTier.WRITE), {"v": "anything"})
        # A gate that cannot be evaluated must become a human decision, not a
        # silently absent check.
        assert decision.requires_approval is True
        assert any("unevaluable" in rule for rule in decision.matched_rules)
        assert "could not be evaluated" in decision.reason

    def test_the_shipped_policy_file_parses_and_defines_rules(self):
        guard = PolicyGuard.from_file(SETTINGS.company_dir / "policies.yaml")
        assert guard.autonomy == "standard"
        assert len(guard.rules) >= 3
        assert "Northwind Components Pvt Ltd" in guard.data["vendors"]["approved"]

    def test_the_shipped_policy_blocks_a_ledger_post_for_an_unapproved_vendor(self):
        guard = PolicyGuard.from_file(SETTINGS.company_dir / "policies.yaml")
        tool = _Tool("ledger_record", RiskTier.SENSITIVE, surface="financeops", reversible=False)
        denied = guard.assess(tool, {"vendor": "Zenith Office Supplies", "amount": 23400})
        assert denied.allowed is False
        assert "FIN-004" in denied.reason or "FIN-004" in " ".join(denied.citations)

    def test_the_shipped_policy_gates_the_controller_account(self):
        guard = PolicyGuard.from_file(SETTINGS.company_dir / "policies.yaml")
        tool = _Tool("erp_login", RiskTier.SENSITIVE, surface="erp")
        decision = guard.assess(tool, {"account": "finance_controller"})
        assert decision.requires_approval is True
        assert "FIN-002" in decision.reason or "FIN-002" in " ".join(decision.citations)


class TestLookupPath:
    def test_dotted_access(self):
        assert lookup_path({"a": {"b": {"c": 1}}}, "a.b.c") == 1

    def test_list_index(self):
        assert lookup_path({"a": [{"b": 2}, {"b": 3}]}, "a[1].b") == 3

    def test_nested_indexes(self):
        assert lookup_path({"a": [[10, 20]]}, "a[0][1]") == 20

    def test_a_missing_key_raises(self):
        with pytest.raises(UnresolvedReference, match="missing key"):
            lookup_path({"a": 1}, "b")

    def test_a_bad_index_raises(self):
        with pytest.raises(UnresolvedReference, match="bad index"):
            lookup_path({"a": [1]}, "a[5]")


class TestResolveRefs:
    def test_a_whole_string_reference_preserves_the_type(self):
        context = {"facts": {"total": 186000}}
        assert resolve_refs("${facts.total}", context) == 186000
        assert isinstance(resolve_refs("${facts.total}", context), int)

    def test_an_embedded_reference_is_interpolated_as_text(self):
        context = {"facts": {"po": "PO-4490"}}
        assert resolve_refs("variance against ${facts.po}", context) == "variance against PO-4490"

    def test_nested_structures_are_resolved_throughout(self):
        context = {"facts": {"n": "INV-1", "amt": 42}}
        resolved = resolve_refs(
            {"invoice": "${facts.n}", "lines": [{"amount": "${facts.amt}"}]}, context
        )
        assert resolved == {"invoice": "INV-1", "lines": [{"amount": 42}]}

    def test_non_strings_pass_through_untouched(self):
        assert resolve_refs({"a": 1, "b": None, "c": True}, {}) == {"a": 1, "b": None, "c": True}

    def test_an_unresolvable_reference_raises_rather_than_passing_a_literal(self):
        # Sending the literal "${facts.missing}" to a tool would write that
        # string into an ERP field, which is worse than failing the step.
        with pytest.raises(UnresolvedReference):
            resolve_refs("${facts.missing}", {"facts": {}})

    def test_step_observations_are_addressable(self):
        state = RunState(run_id="r", goal="g")
        state.plan = Plan(
            steps=[
                Step(
                    id="s1",
                    intent="read",
                    tool="t",
                    status=StepStatus.SUCCEEDED,
                    observation=Observation(ok=True, summary="done", data={"total": 128400}),
                )
            ]
        )
        context = build_ref_context(state)
        assert resolve_refs("${steps.s1.data.total}", context) == 128400
        assert resolve_refs("${steps.s1.ok}", context) is True


class TestBudget:
    def test_each_limit_is_reported_by_name(self):
        from centralign.runtime.state import Budget

        assert Budget(max_steps=1, steps_used=1).exceeded().startswith("step limit")
        assert Budget(max_llm_calls=2, llm_calls_used=2).exceeded().startswith("model-call limit")
        assert (
            Budget(max_consecutive_failures=2, consecutive_failures=2)
            .exceeded()
            .startswith("consecutive-failure limit")
        )
        assert Budget(max_replans=1, replans_used=2).exceeded().startswith("replan limit")

    def test_a_fresh_budget_is_not_exceeded(self):
        from centralign.runtime.state import Budget

        assert Budget().exceeded() is None


class TestConnectorSpecValidation:
    def test_a_connector_without_operations_is_rejected_loudly(self, tmp_path):
        from centralign.tools.connector import ConnectorSpecError, load_connector_file

        path = tmp_path / "bad.yaml"
        path.write_text(yaml.safe_dump({"name": "x", "base_url_setting": "api_url"}), encoding="utf-8")
        with pytest.raises(ConnectorSpecError, match="no operations"):
            load_connector_file(path)

    def test_a_connector_without_a_name_is_rejected(self, tmp_path):
        from centralign.tools.connector import ConnectorSpecError, load_connector_file

        path = tmp_path / "bad.yaml"
        path.write_text(yaml.safe_dump({"base_url_setting": "api_url", "operations": []}), encoding="utf-8")
        with pytest.raises(ConnectorSpecError, match="name"):
            load_connector_file(path)

    def test_the_shipped_connectors_define_the_expected_operations(self):
        """Two whole internal systems, taught by YAML alone."""
        from centralign.tools.connector import load_connectors

        tools = {tool.name: tool for tool in load_connectors(SETTINGS.company_dir / "connectors")}
        by_service: dict[str, set[str]] = {}
        for tool in load_connectors(SETTINGS.company_dir / "connectors"):
            by_service.setdefault(tool.service, set()).add(tool.name)

        assert by_service["financeops"] == {
            "vendor_lookup",
            "gl_code_lookup",
            "ledger_record",
            "ledger_list",
        }
        assert by_service["itops"] == {
            "account_lookup",
            "asset_lookup",
            "access_group_lookup",
        }

        assert tools["ledger_record"].method == "POST"
        assert tools["ledger_record"].status_map[503] == "unavailable"
        assert tools["ledger_record"].status_map[404] == "not_found"

    def test_the_itops_connector_declares_no_writes(self):
        """The write gap is deliberate: those changes are desktop-GUI only.

        A connector that advertised a write endpoint the service does not have
        would send the planner down a path that can only ever 404.
        """
        from centralign.tools.connector import load_connectors

        itops = [t for t in load_connectors(SETTINGS.company_dir / "connectors") if t.service == "itops"]
        assert itops
        assert all(tool.method == "GET" for tool in itops)
        assert all(tool.risk is RiskTier.READ for tool in itops)


class TestToolValidation:
    async def test_a_missing_required_argument_is_an_invalid_args_failure(self):
        from centralign.tools.core_tools import NoteFact

        observation = await NoteFact().invoke({"value": 1}, None)  # type: ignore[arg-type]
        assert not observation.ok
        assert observation.error_kind == "invalid_args"
        assert "key" in (observation.error or "")

    async def test_an_unknown_argument_is_rejected_with_the_accepted_list(self):
        from centralign.tools.drive_tools import ReadDocument

        observation = await ReadDocument().invoke({"path": "a", "bogus": 1}, None)  # type: ignore[arg-type]
        assert not observation.ok
        assert observation.error_kind == "invalid_args"
        assert "accepts only" in (observation.error or "")

    async def test_a_wrong_type_is_reported_with_both_types(self):
        from centralign.tools.drive_tools import ReadDocument

        observation = await ReadDocument().invoke({"path": 123}, None)  # type: ignore[arg-type]
        assert not observation.ok
        assert "should be string" in (observation.error or "")
