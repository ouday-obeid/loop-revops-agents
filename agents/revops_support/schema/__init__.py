"""Schema change path: proposer → sandbox test → metadata deploy → rollback.

Phase 1 Week 2 delivery. Week 1 ships only `cooldown_poller.py` (scheduled
job that elevates approved_primary `sf_schema_delete` gates to a
confirmation gate after the 24h cooling period).
"""
