"""Merge-blocker: `deal_desk/policy.yaml` must be fully signed before merge.

This test intentionally fails while any `UNSIGNED` sentinel remains in the
file. Henry + Anand replace the sentinels with real values (and record an
approval_gate_id per section) to unblock the branch.
"""
from __future__ import annotations

from pathlib import Path

import yaml

POLICY_PATH = (
    Path(__file__).parent.parent / "deal_desk" / "policy.yaml"
)

SENTINEL = "UNSIGNED"


def _walk(node, path: str = "") -> list[str]:
    """Return a list of dotted paths where SENTINEL appears as a value."""
    hits: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            hits.extend(_walk(v, f"{path}.{k}" if path else str(k)))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            hits.extend(_walk(v, f"{path}[{i}]"))
    elif node == SENTINEL:
        hits.append(path or "<root>")
    return hits


def test_policy_file_exists_and_parses():
    assert POLICY_PATH.exists(), f"policy.yaml missing at {POLICY_PATH}"
    data = yaml.safe_load(POLICY_PATH.read_text())
    assert isinstance(data, dict), "policy.yaml must be a top-level mapping"
    for section in ("territory", "quota", "commissions"):
        assert section in data, f"policy.yaml missing section: {section}"


def test_policy_fully_signed():
    """FAILS until Henry + Anand sign every section. Do not weaken."""
    data = yaml.safe_load(POLICY_PATH.read_text())
    unsigned = _walk(data)
    assert not unsigned, (
        "policy.yaml has unsigned values at: "
        + ", ".join(unsigned)
        + " — Henry + Anand must replace every UNSIGNED sentinel before merge."
    )
