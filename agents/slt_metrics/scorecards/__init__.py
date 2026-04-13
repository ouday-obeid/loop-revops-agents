"""Scorecards — AE, SDR, quota.

Every module here is CRUD-plus-aggregation on SF data + the
`pipeline_snapshots` / `rep_config` tables. No Fireflies, no BigQuery —
call intel flows in from the scorer side, and board metrics live in
`board_metrics/`.
"""
