"""Routing — SDR territory assignment.

Rules (from config/territory.yaml):
  ENT (50+ locations)   → Charles's team, round-robin
  MM  (10–49 locations) → Nate's team, round-robin
  SMB (<10 locations)   → Hutch/Henry queue

Skips inactive users; falls back to default_owner_id if whole team is out.
Round-robin state persisted in tof_routing_state (local SQLite).

Ships D6.
"""
from __future__ import annotations
