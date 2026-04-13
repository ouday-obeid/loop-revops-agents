"""Data-quality path: dedup, bulk updates, conversion fixes, validation.

Phase 1 Week 3 delivery. Week 3 Day 13 ships `bulk_updater.py` — replaces the
Phase 0 simulated `salesforce_mcp.bulk_update` with real composite-API execution
(200 rows/chunk) and mandatory pre-write snapshot to `audit_log.before_value`
for rollback.
"""
