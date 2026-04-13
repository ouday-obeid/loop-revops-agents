"""Integration health monitors for the RevOps Support agent.

Each module in this package is a read-only poller that:
  1. Gathers evidence from SF (Tooling API, standard SOQL) or external systems
  2. Classifies what it found into a per-check `status`
  3. When a problem is detected, opens (or reuses) a pending task in the
     `tasks` table so O sees it in the morning brief

No writes to Salesforce. No approvals required. These monitors surface — they
do not remediate.
"""
