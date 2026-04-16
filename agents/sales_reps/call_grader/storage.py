"""Call-grade SQLite persistence — one table, lazy-created on first use.

Lives in the same engine Phase 0 configured (sqlite in dev, postgres in Phase 4).
Idempotent CREATE IF NOT EXISTS; no shared/* schema edit required.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from shared.db.connection import get_engine


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sales_reps_call_grades (
    id INTEGER PRIMARY KEY,
    meeting_id TEXT NOT NULL UNIQUE,
    rep_email TEXT,
    rep_name TEXT,
    call_type TEXT NOT NULL,
    scorecard_type TEXT NOT NULL,
    section_scores TEXT NOT NULL,
    weighted_total REAL,
    max_weighted REAL,
    percentage REAL,
    pass_fail TEXT,
    evidence TEXT,
    feedback TEXT,
    strengths TEXT,
    improvements TEXT,
    critical_misses TEXT,
    coaching_summary TEXT,
    cell_notes TEXT,
    model_used TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd REAL,
    transcript_url TEXT,
    call_date TIMESTAMP,
    graded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_grades_rep_date ON sales_reps_call_grades(rep_email, graded_at)",
    "CREATE INDEX IF NOT EXISTS idx_grades_type_date ON sales_reps_call_grades(call_type, graded_at)",
)


_initialized = False


def ensure_schema() -> None:
    """Create table + indexes on first call. Idempotent.

    `id INTEGER PRIMARY KEY` is portable across SQLite (rowid alias —
    auto-increments) and Postgres (Phase 4 will add an explicit SERIAL
    via Alembic; the column type stays INTEGER). The previous
    `AUTOINCREMENT` keyword was SQLite-only and would fail Postgres
    schema creation.
    """
    global _initialized
    if _initialized:
        return
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(_SCHEMA_SQL))
        for idx in _INDEX_STATEMENTS:
            conn.execute(text(idx))
    _initialized = True


def upsert_grade(grade: dict[str, Any]) -> int:
    """Insert or update by meeting_id. Returns row id."""
    ensure_schema()
    serialized = _serialize_json_fields(grade)
    engine = get_engine()
    with engine.begin() as conn:
        # Use SQLite ON CONFLICT semantics (works on SQLite; Postgres needs ON CONFLICT DO UPDATE).
        # Phase 0 is sqlite; Phase 4 will re-gate.
        conn.execute(
            text(
                """
                INSERT INTO sales_reps_call_grades (
                    meeting_id, rep_email, rep_name, call_type, scorecard_type,
                    section_scores, weighted_total, max_weighted, percentage, pass_fail,
                    evidence, feedback, strengths, improvements, critical_misses,
                    coaching_summary, cell_notes, model_used, tokens_in, tokens_out,
                    cost_usd, transcript_url, call_date
                ) VALUES (
                    :meeting_id, :rep_email, :rep_name, :call_type, :scorecard_type,
                    :section_scores, :weighted_total, :max_weighted, :percentage, :pass_fail,
                    :evidence, :feedback, :strengths, :improvements, :critical_misses,
                    :coaching_summary, :cell_notes, :model_used, :tokens_in, :tokens_out,
                    :cost_usd, :transcript_url, :call_date
                )
                ON CONFLICT(meeting_id) DO UPDATE SET
                    rep_email=excluded.rep_email,
                    rep_name=excluded.rep_name,
                    call_type=excluded.call_type,
                    scorecard_type=excluded.scorecard_type,
                    section_scores=excluded.section_scores,
                    weighted_total=excluded.weighted_total,
                    max_weighted=excluded.max_weighted,
                    percentage=excluded.percentage,
                    pass_fail=excluded.pass_fail,
                    evidence=excluded.evidence,
                    feedback=excluded.feedback,
                    strengths=excluded.strengths,
                    improvements=excluded.improvements,
                    critical_misses=excluded.critical_misses,
                    coaching_summary=excluded.coaching_summary,
                    cell_notes=excluded.cell_notes,
                    model_used=excluded.model_used,
                    tokens_in=excluded.tokens_in,
                    tokens_out=excluded.tokens_out,
                    cost_usd=excluded.cost_usd,
                    transcript_url=excluded.transcript_url,
                    call_date=excluded.call_date,
                    graded_at=CURRENT_TIMESTAMP
                """
            ),
            serialized,
        )
        row = conn.execute(
            text("SELECT id FROM sales_reps_call_grades WHERE meeting_id = :m"),
            {"m": grade["meeting_id"]},
        ).fetchone()
    return int(row[0])


def get_grade(meeting_id: str) -> dict[str, Any] | None:
    ensure_schema()
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT * FROM sales_reps_call_grades WHERE meeting_id = :m"),
            {"m": meeting_id},
        ).mappings().fetchone()
    if not row:
        return None
    return _deserialize_json_fields(dict(row))


def list_grades_for_rep(
    rep_email: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    ensure_schema()
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """SELECT * FROM sales_reps_call_grades
                   WHERE rep_email = :r
                   ORDER BY graded_at DESC LIMIT :l"""
            ),
            {"r": rep_email, "l": limit},
        ).mappings().all()
    return [_deserialize_json_fields(dict(r)) for r in rows]


def grade_exists(meeting_id: str) -> bool:
    ensure_schema()
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT 1 FROM sales_reps_call_grades WHERE meeting_id = :m"),
            {"m": meeting_id},
        ).fetchone()
    return row is not None


# -------------------------------------------------------------- JSON helpers

_JSON_FIELDS = (
    "section_scores", "evidence", "feedback", "strengths",
    "improvements", "critical_misses", "cell_notes",
)


def _serialize_json_fields(grade: dict[str, Any]) -> dict[str, Any]:
    out = dict(grade)
    for field in _JSON_FIELDS:
        val = out.get(field)
        if val is not None and not isinstance(val, str):
            out[field] = json.dumps(val)
    # Ensure all bind params are present even if None
    for field in _ALL_PARAMS:
        out.setdefault(field, None)
    return out


def _deserialize_json_fields(row: dict[str, Any]) -> dict[str, Any]:
    for field in _JSON_FIELDS:
        val = row.get(field)
        if val and isinstance(val, str):
            try:
                row[field] = json.loads(val)
            except json.JSONDecodeError:
                pass
    return row


_ALL_PARAMS = (
    "meeting_id", "rep_email", "rep_name", "call_type", "scorecard_type",
    "section_scores", "weighted_total", "max_weighted", "percentage", "pass_fail",
    "evidence", "feedback", "strengths", "improvements", "critical_misses",
    "coaching_summary", "cell_notes", "model_used", "tokens_in", "tokens_out",
    "cost_usd", "transcript_url", "call_date",
)
