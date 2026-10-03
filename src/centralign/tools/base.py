"""Tool and connector abstraction.

The generalization claim rests entirely on this file. The kernel never names a
tool; it only reads the registry's catalog and dispatches whatever the planner
chose. Teaching the operator a new company workflow therefore means adding a
:class:`Tool` subclass and an SOP document -- no kernel change, no prompt
surgery.

Two things a tool must do beyond "perform the action":

* **Classify its own failures.** ``ToolFailure.kind`` is what lets the adapt
  phase distinguish "retry this, the network blipped" from "stop retrying, the
  PO genuinely does not exist, replan". Collapsing those into one exception type
  is the single most common reason agents loop.
* **Describe itself for a human.** ``preview()`` renders what is about to happen
  in business terms, because an approval prompt reading
  ``http_post(/api/v1/invoices, {...})`` is not consent.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

from ..runtime.state import Observation, RiskTier

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from ..config import Settings
    from ..events.log import EventLog
    from ..memory.store import MemoryStore
    from ..runtime.state import RunState


class FailureKind:
    """Failure taxonomy the adapt phase branches on.

    Deliberately a small closed set of strings rather than an Enum: tools in
    plugins should be able to report a kind without importing an enum, and the
    strings end up in the event log where they need to stay readable.
    """

    NOT_FOUND = "not_found"              # target does not exist -> replan, do not retry
    INVALID_ARGS = "invalid_args"        # our fault -> repair args, then retry
    PRECONDITION = "precondition_failed" # world is not ready -> replan
    PERMISSION = "permission_denied"     # policy or sandbox refused -> escalate to human
    CONFLICT = "conflict"                # duplicate/already-exists -> often already done
    TRANSIENT = "transient"              # retry with backoff
    TIMEOUT = "timeout"                  # retry with backoff
    UNAVAILABLE = "unavailable"          # dependency down -> retry, then escalate
    AMBIGUOUS = "ambiguous"              # needs a human decision
    INTERNAL = "internal"                # bug in the tool itself

    RETRYABLE = {TRANSIENT, TIMEOUT, UNAVAILABLE}


class ToolFailure(Exception):
    """Raised by a tool to report a classified, actionable failure."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = FailureKind.INTERNAL,
        data: dict[str, Any] | None = None,
        hint: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.data = data or {}
        self.hint = hint

    @property
    def retryable(self) -> bool:
        return self.kind in FailureKind.RETRYABLE

    def as_observation(self, duration_ms: int = 0) -> Observation:
        detail = f"{self.message}" + (f" Hint: {self.hint}" if self.hint else "")
        return Observation(
            ok=False,
            summary=detail,
            error=detail,
            error_kind=self.kind,
            retryable=self.retryable,
            data=self.data,
            duration_ms=duration_ms,
        )


class HumanInputRequired(Exception):
    """Raised by a tool that cannot proceed without a person.

    A dedicated exception rather than a flag on :class:`Observation`, because
    this is control flow: the kernel must suspend the run, persist it, and be
    able to resume days later. Making that look like an ordinary observation
    invites the kernel to carry on regardless, which is the failure mode that
    turns a human-in-the-loop design into decoration.
    """

    def __init__(
        self,
        prompt: str,
        *,
        kind: str = "question",
        options: list[str] | None = None,
        context: dict[str, Any] | None = None,
        risk: RiskTier | None = None,
    ) -> None:
        super().__init__(prompt)
        self.prompt = prompt
        self.kind = kind
        self.options = options or []
        self.context = context or {}
        self.risk = risk


@dataclass
class ToolContext:
    """Ambient services a tool may use. Passed in, never imported globally."""

    settings: "Settings"
    run_id: str
    events: "EventLog"
    state: "RunState"
    memory: "MemoryStore"
    artifacts_dir: Path
    #: Lazily-created shared resources, keyed by name, torn down with the run.
    resources: dict[str, Any] = field(default_factory=dict)

    def artifact_path(self, filename: str) -> Path:
        directory = self.artifacts_dir / self.run_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory / filename

    def relative_artifact(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.artifacts_dir)).replace("\\", "/")
        except ValueError:
            return str(path)


class Tool(ABC):
    """A capability the operator can invoke.

    Subclasses declare metadata as class attributes and implement
    :meth:`execute`. The registry turns those attributes into the planner's
    catalog, so the declaration *is* the documentation the model sees.
    """

    name: str = ""
    description: str = ""
    #: JSON Schema for ``args``. Keep it tight: it is both validation and the
    #: planner's only instruction on how to call this tool.
    args_schema: dict[str, Any] = {"type": "object", "properties": {}}
    #: Baseline risk. :meth:`risk_for` may escalate it per-call.
    risk: RiskTier = RiskTier.READ
    #: Whether the effect can be undone by a later step. Drives approval copy.
    reversible: bool = True
    #: Tools that only read can be retried freely; mutating tools need care.
    idempotent: bool = True
    #: Grouping label for the catalog, e.g. "drive", "erp", "human".
    surface: str = "general"

    # -- subclass contract --------------------------------------------------
    @abstractmethod
    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        """Perform the action. Raise :class:`ToolFailure` on a classified error."""

    def risk_for(self, args: dict[str, Any]) -> RiskTier:
        """Risk of this specific invocation. Override to escalate on arguments."""
        return self.risk

    def preview(self, args: dict[str, Any]) -> str:
        """One line a non-engineer can consent to."""
        rendered = ", ".join(f"{k}={_short(v)}" for k, v in args.items())
        return f"{self.name}({rendered})"

    # -- framework use ------------------------------------------------------
    async def invoke(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        """Execute with timing and uniform error capture.

        Every exception becomes an :class:`Observation`. The kernel must always
        get an observation back -- a tool that raises past this boundary would
        kill the run instead of being adapted around.
        """
        started = time.perf_counter()
        try:
            self.validate(args)
            observation = await self.execute(args, ctx)
        except HumanInputRequired:
            # Control flow, not failure. Must reach the kernel so the run can be
            # suspended rather than recorded as a failed step and retried.
            raise
        except ToolFailure as failure:
            return failure.as_observation(_ms(started))
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all boundary
            return Observation(
                ok=False,
                summary=f"{type(exc).__name__}: {exc}",
                error=f"{type(exc).__name__}: {exc}",
                error_kind=FailureKind.INTERNAL,
                retryable=False,
                duration_ms=_ms(started),
            )
        observation.duration_ms = observation.duration_ms or _ms(started)
        return observation

    def validate(self, args: dict[str, Any]) -> None:
        """Check required keys and reject unknown ones.

        A hand-rolled check rather than a jsonschema dependency: we need only
        required/unknown/type, and the error text is tuned to be *useful to the
        model* on the repair attempt, which a generic validator's message is not.
        """
        schema = self.args_schema or {}
        properties: dict[str, Any] = schema.get("properties", {}) or {}
        required: list[str] = schema.get("required", []) or []

        missing = [key for key in required if key not in args or args[key] is None]
        if missing:
            raise ToolFailure(
                f"missing required argument(s): {', '.join(missing)}",
                kind=FailureKind.INVALID_ARGS,
                hint=f"{self.name} requires: {', '.join(required)}",
            )

        if properties and schema.get("additionalProperties") is not True:
            unknown = [key for key in args if key not in properties]
            if unknown:
                raise ToolFailure(
                    f"unknown argument(s): {', '.join(unknown)}",
                    kind=FailureKind.INVALID_ARGS,
                    hint=f"{self.name} accepts only: {', '.join(properties) or '(none)'}",
                )

        for key, value in args.items():
            expected = (properties.get(key) or {}).get("type")
            if expected and value is not None and not _type_ok(value, expected):
                raise ToolFailure(
                    f"argument {key!r} should be {expected}, got {type(value).__name__}",
                    kind=FailureKind.INVALID_ARGS,
                )

    def catalog_entry(self) -> dict[str, Any]:
        """How this tool is presented to the planner."""
        return {
            "name": self.name,
            "surface": self.surface,
            "description": self.description.strip(),
            "args": (self.args_schema or {}).get("properties", {}),
            "required": (self.args_schema or {}).get("required", []),
            "risk": self.risk.value,
            "reversible": self.reversible,
        }


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def _type_ok(value: Any, expected: str) -> bool:
    types = _JSON_TYPES.get(expected)
    if types is None:
        return True
    if expected in {"number", "integer"} and isinstance(value, bool):
        return False  # bool is an int subclass; almost never what a schema means
    return isinstance(value, types)


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _short(value: Any, limit: int = 60) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."
