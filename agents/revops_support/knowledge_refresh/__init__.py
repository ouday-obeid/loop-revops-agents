"""Knowledge refresh pipeline for agents/revops_support.

Sunday 02:00 → metadata_snapshotter writes `var/knowledge_snapshots/<date>/sf_*.md`.
Monday 09:00 → diff_producer compares snapshot vs canonical, DMs O a summary.
O-initiated  → merger copies approved files into the canonical knowledge
               directory and reingests.

Canonical `sf_*.md` files (configured via REVOPS_CANONICAL_KNOWLEDGE_DIR) are
never auto-overwritten — merge only runs after O confirms.
"""
