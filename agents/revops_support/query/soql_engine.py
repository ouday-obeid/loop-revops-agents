"""SOQL engine — thin wrapper around salesforce_mcp.soql_query with guardrails.

Guardrails:
  - default LIMIT if the query omits one (prevents accidental 28K-row scans)
  - reject DDL/DML patterns (SOQL is SELECT-only anyway, but belt-and-braces)
  - audit every invocation (target='sf:soql') so we can reconstruct read load
  - route via intent='read' by default (SF_ORG_ALIAS service user)

This is the read primitive the dispatcher's canned queries and ad-hoc
`@oo revops-support soql <query>` command share.
"""
from __future__ import annotations

import re
from typing import Any

from shared.governance import write_audit
from shared.mcp import salesforce_mcp

_FORBIDDEN = re.compile(r"\b(INSERT|UPDATE|DELETE|UPSERT|MERGE|DROP|ALTER)\b", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\blimit\s+\d+", re.IGNORECASE)


class SOQLError(ValueError):
    pass


def run(
    query: str,
    *,
    agent_name: str = "revops_support",
    default_limit: int = 50,
    audit: bool = True,
    intent: str = "read",
) -> dict[str, Any]:
    """Execute a SOQL SELECT. Enforces LIMIT and rejects DML.

    Returns the raw sf CLI result dict (has 'records', 'totalSize', 'done').
    """
    cleaned = query.strip().rstrip(";")
    if not cleaned:
        raise SOQLError("empty query")
    if not re.match(r"^\s*SELECT\b", cleaned, re.IGNORECASE):
        raise SOQLError("only SELECT queries are allowed via soql_engine.run()")
    if _FORBIDDEN.search(cleaned):
        raise SOQLError("DML/DDL keywords are not permitted in SOQL reads")

    if not _LIMIT_RE.search(cleaned):
        cleaned = f"{cleaned} LIMIT {default_limit}"

    result = salesforce_mcp.soql_query(cleaned, limit=default_limit, intent=intent)
    if audit:
        write_audit(
            agent_name=agent_name,
            action="sf_soql",
            target="sf:soql",
            after={"query": cleaned[:500], "count": len(result.get("records", []))},
        )
    return result
