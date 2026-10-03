"""Policy engine: what the operator may do on its own.

This is the system's security boundary, and it is deliberately **not** a prompt.
The model proposes actions; this module decides whether each one may proceed
unattended. Risk is computed here from the tool's declared tier plus declarative
rules over the actual arguments. The model never asserts its own risk level,
because a model that could would eventually talk itself past every gate -- by
accident if not by prompt injection from a document it read.

Rules live in ``company/policies.yaml`` so that deploying into a different
company means editing data, not code. The evaluator below is intentionally
small and total: every rule either matches or does not, there is no scripting,
and an unparseable rule fails closed by escalating to approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..runtime.state import RiskTier
from ..tools.base import Tool

#: Ordering for "at least this risky" comparisons.
_RISK_ORDER = {
    RiskTier.READ: 0,
    RiskTier.WRITE: 1,
    RiskTier.SENSITIVE: 2,
    RiskTier.EXTERNAL: 3,
}

#: Autonomy level -> the lowest risk tier that still needs a human.
_AUTONOMY_GATE = {
    "supervised": RiskTier.WRITE,
    "standard": RiskTier.SENSITIVE,
    "high": RiskTier.EXTERNAL,
}


@dataclass
class PolicyDecision:
    allowed: bool
    risk: RiskTier
    requires_approval: bool
    reasons: list[str] = field(default_factory=list)
    matched_rules: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return " ".join(self.reasons) if self.reasons else "No policy rule applied."


class PolicyGuard:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self.autonomy: str = str(config.get("autonomy", "standard")).lower()
        self.rules: list[dict[str, Any]] = list(config.get("rules") or [])
        self.data: dict[str, Any] = dict(config.get("data") or {})
        self.denied_tools: set[str] = set(config.get("denied_tools") or [])

    @classmethod
    def from_file(cls, path: Path) -> "PolicyGuard":
        if not path.exists():
            return cls()
        try:
            config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            # Fail closed: an unreadable policy file must not mean "no policy".
            return cls({"autonomy": "supervised"})
        return cls(config)

    @property
    def gate(self) -> RiskTier:
        return _AUTONOMY_GATE.get(self.autonomy, RiskTier.SENSITIVE)

    def assess(self, tool: Tool, args: dict[str, Any]) -> PolicyDecision:
        """Decide whether this specific invocation may proceed unattended."""
        if tool.name in self.denied_tools:
            return PolicyDecision(
                allowed=False,
                risk=RiskTier.EXTERNAL,
                requires_approval=False,
                reasons=[f"{tool.name} is denied by company policy."],
                matched_rules=["denied_tools"],
            )

        risk = tool.risk_for(args)
        reasons: list[str] = []
        matched: list[str] = []
        citations: list[str] = []
        force_approval = False
        #: An explicit grant of autonomy *below* the baseline gate. Without this
        #: the DSL could only ever raise the bar, so a company could not say
        #: "posting an approved invoice to the ledger is routine" -- every
        #: sensitive action would need a human forever, and the operator would
        #: not be autonomous in any useful sense.
        explicit_allow = False

        for rule in self.rules:
            rule_id = str(rule.get("id", "unnamed"))
            try:
                if not self._matches(rule.get("match") or {}, tool, args, risk):
                    continue
            except Exception:  # noqa: BLE001 - a broken rule must fail closed
                force_approval = True
                matched.append(f"{rule_id} (unevaluable, escalated)")
                reasons.append(f"Rule {rule_id} could not be evaluated; escalating.")
                continue

            effect = rule.get("effect") or {}
            matched.append(rule_id)
            if effect.get("reason"):
                reasons.append(str(effect["reason"]))
            if effect.get("cites"):
                citations.append(str(effect["cites"]))

            escalated = _as_risk(effect.get("risk"))
            if escalated and _RISK_ORDER[escalated] > _RISK_ORDER[risk]:
                risk = escalated
            if effect.get("require_approval"):
                force_approval = True
            if effect.get("allow_without_approval"):
                explicit_allow = True
            if effect.get("deny"):
                return PolicyDecision(
                    allowed=False,
                    risk=risk,
                    requires_approval=False,
                    reasons=reasons or [f"Denied by rule {rule_id}."],
                    matched_rules=matched,
                    citations=citations,
                )

        # Precedence: deny > require_approval > allow_without_approval > baseline
        # gate. A rule that demands a human always beats one that grants autonomy,
        # so the two cannot be combined into an accidental bypass.
        if force_approval:
            needs_approval = True
        elif explicit_allow:
            needs_approval = False
        else:
            needs_approval = _RISK_ORDER[risk] >= _RISK_ORDER[self.gate]

        if needs_approval and not reasons:
            reasons.append(
                f"Risk tier '{risk.value}' meets the approval gate for autonomy "
                f"level '{self.autonomy}'."
            )

        return PolicyDecision(
            allowed=True,
            risk=risk,
            requires_approval=needs_approval,
            reasons=reasons,
            matched_rules=matched,
            citations=citations,
        )

    # -- rule matching ------------------------------------------------------
    def _matches(
        self, match: dict[str, Any], tool: Tool, args: dict[str, Any], risk: RiskTier
    ) -> bool:
        """Every declared condition must hold (AND). An empty match never fires."""
        if not match:
            return False

        if "tools" in match and tool.name not in (match["tools"] or []):
            return False
        if "surfaces" in match and tool.surface not in (match["surfaces"] or []):
            return False
        if "min_risk" in match:
            floor = _as_risk(match["min_risk"])
            if floor is None or _RISK_ORDER[risk] < _RISK_ORDER[floor]:
                return False
        if "irreversible" in match and bool(match["irreversible"]) != (not tool.reversible):
            return False

        if "arg" in match:
            condition = match["arg"] or {}
            value = args.get(condition.get("name"))
            if value is None:
                return False
            if not _numeric_condition(value, condition):
                return False

        if "arg_not_in" in match:
            condition = match["arg_not_in"] or {}
            value = args.get(condition.get("name"))
            allowed = self._resolve_data(condition.get("allow_from", ""))
            if not isinstance(allowed, list):
                # A rule whose allowlist does not resolve is a misconfiguration,
                # not a non-match. Returning False here would silently disable
                # the check -- the gate would simply stop existing, and nothing
                # would say so. Raise, and let the caller escalate.
                raise ValueError(
                    f"allow_from {condition.get('allow_from')!r} does not resolve to a list"
                )
            # Fires when the value is absent from the allowlist -- the whole
            # point is to catch the unapproved vendor, not the approved one.
            if value is None:
                return True
            return _normalize(value) not in {_normalize(item) for item in allowed}

        if "arg_equals" in match:
            condition = match["arg_equals"] or {}
            if _normalize(args.get(condition.get("name"))) != _normalize(condition.get("value")):
                return False

        return True

    def _resolve_data(self, dotted: str) -> Any:
        current: Any = self.data
        for part in str(dotted).split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    def describe(self) -> dict[str, Any]:
        return {
            "autonomy": self.autonomy,
            "approval_gate": self.gate.value,
            "rule_count": len(self.rules),
            "denied_tools": sorted(self.denied_tools),
        }


def _numeric_condition(value: Any, condition: dict[str, Any]) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    for operator, threshold in (
        ("gte", lambda n, t: n >= t),
        ("gt", lambda n, t: n > t),
        ("lte", lambda n, t: n <= t),
        ("lt", lambda n, t: n < t),
    ):
        if operator in condition and not threshold(number, float(condition[operator])):
            return False
    return any(key in condition for key in ("gte", "gt", "lte", "lt"))


def _as_risk(value: Any) -> RiskTier | None:
    if isinstance(value, RiskTier):
        return value
    if not value:
        return None
    try:
        return RiskTier(str(value).lower())
    except ValueError:
        return None


def _normalize(value: Any) -> str:
    return str(value).strip().lower()
