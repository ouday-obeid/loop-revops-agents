"""slt_metrics tests rely on the session-autouse _isolate_db fixture in
tests/conftest.py. It applies here because pyproject.toml testpaths lists
both "tests" and "agents/slt_metrics/tests" — pytest collects them in one
session, so the session-scoped fixture fires once and all tests share the
isolated DB. pytest does NOT walk up from a sibling directory to find it.

If this agent later ships a migration that modifies an existing table
(pre-0004), copy the per-agent _isolate_db pattern from
agents/revops_support/tests/conftest.py to guarantee a fresh DB — see
memory entry "per-agent test DB bootstrap required" for the incident.
"""
