"""Drive the cross-agent import checker directly."""
from unittest.mock import MagicMock

from shared.lint.import_rules import CrossAgentImportChecker


def _make_node(path: str, modname: str = ""):
    node = MagicMock()
    root = MagicMock()
    root.file = path
    node.root.return_value = root
    node.modname = modname
    node.names = [(modname, None)] if modname else []
    return node


def test_violation_detected():
    linter = MagicMock()
    checker = CrossAgentImportChecker(linter)
    node = _make_node("/tmp/repo/agents/top_of_funnel/foo.py", "agents.sales_reps.bar")
    checker.visit_importfrom(node)
    linter.add_message.assert_called()


def test_same_agent_allowed():
    linter = MagicMock()
    checker = CrossAgentImportChecker(linter)
    node = _make_node("/tmp/repo/agents/oo/x.py", "agents.oo.classifier")
    checker.visit_importfrom(node)
    linter.add_message.assert_not_called()


def test_shared_import_allowed():
    linter = MagicMock()
    checker = CrossAgentImportChecker(linter)
    node = _make_node("/tmp/repo/agents/oo/x.py", "shared.governance")
    checker.visit_importfrom(node)
    linter.add_message.assert_not_called()


def test_outside_agents_dir_ignored():
    linter = MagicMock()
    checker = CrossAgentImportChecker(linter)
    node = _make_node("/tmp/repo/scripts/foo.py", "agents.oo.x")
    checker.visit_importfrom(node)
    linter.add_message.assert_not_called()


def test_visit_import_form():
    linter = MagicMock()
    checker = CrossAgentImportChecker(linter)
    node = _make_node("/tmp/repo/agents/a/x.py", "agents.b.y")
    node.names = [("agents.b.y", None)]
    checker.visit_import(node)
    linter.add_message.assert_called()
