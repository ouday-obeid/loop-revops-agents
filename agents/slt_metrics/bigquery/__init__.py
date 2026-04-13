"""Loop Pulse (BigQuery) client + queries.

Gap-flag-first by design: the client probes for creds at init and returns
False from `is_connected()` when they're missing, so every board-metric path
degrades gracefully to `-- (Loop Pulse unavailable)` cells until O wires the
service-account credentials into `BQ_CREDENTIALS_JSON`.
"""
