"""Pylint plugin: forbid cross-agent imports.

A file under agents/X/ may import from shared.* and agents.X.* only —
not from agents.Y.* where Y != X. This keeps specialists isolated so a bug
in one can't cascade.

Usage:
    pylint --load-plugins shared.lint.import_rules <targets>
"""
from __future__ import annotations

import re

from pylint.checkers import BaseChecker

_AGENT_PATH = re.compile(r"(?:^|/)agents/(?P<agent>[^/]+)/")


class CrossAgentImportChecker(BaseChecker):
    name = "cross-agent-import"
    priority = -1
    msgs = {
        "E9001": (
            "Cross-agent import from agents.%s into agents/%s/ is forbidden; use shared/ instead",
            "cross-agent-import",
            "Specialist agents must not import each other's code.",
        ),
    }

    def _current_agent(self, node) -> str | None:
        path = getattr(node.root(), "file", "") or ""
        m = _AGENT_PATH.search(path.replace("\\", "/"))
        return m.group("agent") if m else None

    def _check_module(self, modname: str, node) -> None:
        if not modname or not modname.startswith("agents."):
            return
        parts = modname.split(".")
        if len(parts) < 2:
            return
        imported_agent = parts[1]
        current = self._current_agent(node)
        if current is None:
            return
        if imported_agent != current:
            self.add_message("cross-agent-import", node=node, args=(imported_agent, current))

    def visit_import(self, node) -> None:
        for modname, _ in node.names:
            self._check_module(modname, node)

    def visit_importfrom(self, node) -> None:
        self._check_module(node.modname, node)


def register(linter) -> None:
    linter.register_checker(CrossAgentImportChecker(linter))
