"""The tool registry and the catalog it renders for the planner."""

from __future__ import annotations

import json
from typing import Iterable

from .base import FailureKind, Tool, ToolFailure


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> Tool:
        if not tool.name:
            raise ValueError(f"{type(tool).__name__} has no name")
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name {tool.name!r}")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolFailure(
                f"no such tool {name!r}",
                kind=FailureKind.NOT_FOUND,
                hint=f"available tools: {', '.join(sorted(self._tools))}",
            )
        return tool

    def has(self, name: str) -> bool:
        return name in self._tools

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self):
        return iter(self._tools.values())

    def catalog(self) -> list[dict]:
        return [tool.catalog_entry() for tool in self._tools.values()]

    def render_catalog(self) -> str:
        """Catalog as a compact prompt block, grouped by surface.

        Grouping matters more than it looks: it gives the planner a mental model
        of *where* work happens (drive, ERP, API, human) rather than a flat list
        of twenty verbs, and it noticeably reduces cross-surface confusion like
        reaching for an HTTP call when a browser step is required.
        """
        by_surface: dict[str, list[dict]] = {}
        for entry in self.catalog():
            by_surface.setdefault(entry["surface"], []).append(entry)

        blocks: list[str] = []
        for surface in sorted(by_surface):
            lines = [f"## surface: {surface}"]
            for entry in sorted(by_surface[surface], key=lambda e: e["name"]):
                args = json.dumps(entry["args"], ensure_ascii=False)
                required = ", ".join(entry["required"]) or "-"
                lines.append(
                    f"- {entry['name']} (risk={entry['risk']}"
                    f"{'' if entry['reversible'] else ', irreversible'})\n"
                    f"    {entry['description']}\n"
                    f"    args={args}\n"
                    f"    required={required}"
                )
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)
