"""Declarative HTTP connectors.

This file is the generalization argument made concrete. Nowhere does it mention
vendors, invoices or ledgers -- those live in ``company/connectors/*.yaml``.
Adding a new internal system to the operator's repertoire means writing a YAML
file describing its endpoints; no Python, no kernel change, no prompt edit. The
tools appear in the planner's catalog on next start.

The DSL is deliberately small. It covers method, templated path, query and body
mapping, an argument schema, a risk tier, and -- the part that matters most for
reliability -- a mapping from HTTP status to this system's
:class:`~centralign.tools.base.FailureKind` taxonomy. Without that last piece a
404 and a 503 look identical to the adapt phase, and it would retry the one that
can never succeed while giving up on the one that would.

A more expressive DSL (conditionals, loops, response transforms) was a
deliberate non-goal: anything that needs those is better expressed as a Python
:class:`~centralign.tools.base.Tool`, which is already a supported extension
point. A connector DSL that grows into a programming language is a liability.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import yaml

from ..runtime.state import Observation, RiskTier
from .base import FailureKind, Tool, ToolContext, ToolFailure

#: Default status -> failure kind mapping, overridable per operation.
_DEFAULT_STATUS_MAP = {
    400: FailureKind.INVALID_ARGS,
    401: FailureKind.PERMISSION,
    403: FailureKind.PERMISSION,
    404: FailureKind.NOT_FOUND,
    409: FailureKind.CONFLICT,
    422: FailureKind.INVALID_ARGS,
    429: FailureKind.TRANSIENT,
    500: FailureKind.TRANSIENT,
    502: FailureKind.TRANSIENT,
    503: FailureKind.UNAVAILABLE,
    504: FailureKind.TIMEOUT,
}


class HttpConnectorTool(Tool):
    """One operation of one declaratively-described HTTP service."""

    def __init__(self, *, service: str, base_url_setting: str, spec: dict[str, Any]) -> None:
        self.service = service
        self.base_url_setting = base_url_setting
        self.spec = spec

        self.name = str(spec["name"])
        self.description = str(spec.get("description", "")).strip()
        self.surface = str(spec.get("surface", service))
        self.method = str(spec.get("method", "GET")).upper()
        self.path_template = str(spec.get("path", "/"))
        self.query_spec: dict[str, Any] = spec.get("query") or {}
        self.body_spec: list[str] = list(spec.get("body") or [])
        self.success_codes: set[int] = set(spec.get("success_codes") or [])
        self.status_map: dict[int, str] = {
            **_DEFAULT_STATUS_MAP,
            **{int(k): str(v) for k, v in (spec.get("status_map") or {}).items()},
        }
        self.risk = _risk(spec.get("risk"), default=RiskTier.READ)
        self.reversible = bool(spec.get("reversible", True))
        self.idempotent = bool(spec.get("idempotent", self.method in {"GET", "HEAD", "PUT"}))
        self.args_schema = {
            "type": "object",
            "properties": spec.get("args") or {},
            "required": list(spec.get("required") or []),
        }
        self.preview_template = str(spec.get("preview", ""))

    def preview(self, args: dict[str, Any]) -> str:
        if self.preview_template:
            try:
                return self.preview_template.format(**args)
            except (KeyError, IndexError):
                pass
        return f"{self.method} {self.service}{self.path_template} with {json.dumps(args)[:160]}"

    def _base_url(self, ctx: ToolContext) -> str:
        url = getattr(ctx.settings, self.base_url_setting, None)
        if not url:
            raise ToolFailure(
                f"connector {self.service!r} references unknown setting "
                f"{self.base_url_setting!r}",
                kind=FailureKind.INTERNAL,
            )
        return str(url).rstrip("/")

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        try:
            path = self.path_template.format(**args)
        except KeyError as exc:
            raise ToolFailure(
                f"path template needs argument {exc.args[0]!r}", kind=FailureKind.INVALID_ARGS
            ) from exc

        url = f"{self._base_url(ctx)}/{path.lstrip('/')}"
        params = {
            name: args.get(mapping.get("arg", name))
            for name, mapping in self.query_spec.items()
            if args.get(mapping.get("arg", name)) is not None
        }
        body = {key: args[key] for key in self.body_spec if key in args} or None

        client: httpx.AsyncClient = ctx.resources.get("http_client")  # type: ignore[assignment]
        if client is None:
            client = httpx.AsyncClient(timeout=httpx.Timeout(20.0))
            ctx.resources["http_client"] = client

        try:
            response = await client.request(self.method, url, params=params, json=body)
        except httpx.TimeoutException as exc:
            raise ToolFailure(
                f"{self.service} timed out calling {self.method} {path}",
                kind=FailureKind.TIMEOUT,
                hint="retrying is appropriate",
            ) from exc
        except httpx.TransportError as exc:
            raise ToolFailure(
                f"could not reach {self.service} at {url}: {exc}",
                kind=FailureKind.UNAVAILABLE,
                hint="is the sandbox running? 'python -m sandbox.serve'",
            ) from exc

        payload = _decode(response)
        ok = (
            response.status_code in self.success_codes
            if self.success_codes
            else 200 <= response.status_code < 300
        )

        if not ok:
            kind = self.status_map.get(response.status_code, FailureKind.INTERNAL)
            detail = _detail(payload) or response.text[:300]
            raise ToolFailure(
                f"{self.service} {self.method} {path} -> HTTP {response.status_code}: {detail}",
                kind=kind,
                data={"status": response.status_code, "response": payload},
                hint=str(self.spec.get("on_failure_hint", "")),
            )

        return Observation(
            ok=True,
            summary=(
                f"{self.service} {self.method} {path} -> HTTP {response.status_code}\n"
                f"{json.dumps(payload, indent=2, ensure_ascii=False)[:2500]}"
            ),
            data={"status": response.status_code, "response": payload},
        )


def _decode(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError):
        return {"raw": response.text[:2000]}


def _detail(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("detail", "message", "error"):
            if payload.get(key):
                return str(payload[key])
    return ""


def _risk(value: Any, *, default: RiskTier) -> RiskTier:
    if not value:
        return default
    try:
        return RiskTier(str(value).lower())
    except ValueError:
        return default


class ConnectorSpecError(ValueError):
    """A connector file is malformed. Raised loudly: a silently skipped
    connector presents as a mysteriously incapable agent at run time."""


def load_connector_file(path: Path) -> list[HttpConnectorTool]:
    try:
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConnectorSpecError(f"{path.name}: invalid YAML: {exc}") from exc

    service = spec.get("name")
    if not service:
        raise ConnectorSpecError(f"{path.name}: missing top-level 'name'")
    base_url_setting = spec.get("base_url_setting")
    if not base_url_setting:
        raise ConnectorSpecError(f"{path.name}: missing 'base_url_setting'")

    operations = spec.get("operations") or []
    if not operations:
        raise ConnectorSpecError(f"{path.name}: declares no operations")

    tools: list[HttpConnectorTool] = []
    for operation in operations:
        if not operation.get("name"):
            raise ConnectorSpecError(f"{path.name}: an operation is missing 'name'")
        tools.append(
            HttpConnectorTool(
                service=str(service),
                base_url_setting=str(base_url_setting),
                spec={"surface": spec.get("surface", service), **operation},
            )
        )
    return tools


def load_connectors(directory: Path) -> list[HttpConnectorTool]:
    """Load every ``*.yaml`` connector in ``directory``."""
    if not directory.exists():
        return []
    tools: list[HttpConnectorTool] = []
    for path in sorted(directory.glob("*.y*ml")):
        tools.extend(load_connector_file(path))
    return tools
