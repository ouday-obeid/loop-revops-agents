"""CSV parser for `@oo slt ingest-rep-forecast` submissions.

Input CSV (header required):

    rep_name,quarter,commit_acv,best_case_acv,notes
    Sarra Herlich,FY2026-Q2,250000,400000,ramping

Rejections are per-row — a typo in one row does not kill the whole file.
The parser returns both a list of parseable `RepForecastEntry` records and
a list of rejection dicts so the dispatcher can report both counts back
to the user.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Iterable

from agents.slt_metrics.pipeline.planning import AE_ROSTER, SDR_ROSTER
from agents.slt_metrics.types import RepForecastEntry


_REQUIRED_COLUMNS = frozenset({"rep_name", "quarter"})
_QUARTER_PATTERN = re.compile(r"^FY\d{4}-Q[1-4]$")


def _known_owner_names() -> frozenset[str]:
    return frozenset(
        entry.name for entry in (*AE_ROSTER, *SDR_ROSTER)
    )


def _clean_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    s = raw.strip().replace(",", "").replace("$", "")
    if not s:
        return None
    return float(s)


def parse_rep_forecast_csv(
    path: str | Path,
    *,
    known_owners: Iterable[str] | None = None,
) -> tuple[list[RepForecastEntry], list[dict]]:
    """Parse a rep-forecast CSV file.

    Returns `(entries, errors)`. `errors` is a list of dicts shaped like
    `{"row": int, "reason": str, "raw": dict}`. `known_owners` defaults to
    the union of `AE_ROSTER` + `SDR_ROSTER` names; pass an explicit set to
    accept custom rosters (e.g. during tests).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"rep-forecast CSV not found: {path}")

    allowed = frozenset(known_owners) if known_owners is not None else _known_owner_names()

    entries: list[RepForecastEntry] = []
    errors: list[dict] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or not _REQUIRED_COLUMNS.issubset(reader.fieldnames):
            missing = _REQUIRED_COLUMNS - set(reader.fieldnames or [])
            raise ValueError(
                f"rep-forecast CSV missing required columns: {sorted(missing)}"
            )
        for line_no, row in enumerate(reader, start=2):  # header is line 1
            rep_name = (row.get("rep_name") or "").strip()
            quarter = (row.get("quarter") or "").strip()
            if not rep_name:
                errors.append({"row": line_no, "reason": "empty rep_name", "raw": row})
                continue
            if rep_name not in allowed:
                errors.append({
                    "row": line_no,
                    "reason": f"rep_name '{rep_name}' not in AE/SDR roster",
                    "raw": row,
                })
                continue
            if not _QUARTER_PATTERN.match(quarter):
                errors.append({
                    "row": line_no,
                    "reason": f"quarter '{quarter}' must match FY####-Q#",
                    "raw": row,
                })
                continue
            try:
                commit_acv = _clean_float(row.get("commit_acv"))
                best_case_acv = _clean_float(row.get("best_case_acv"))
            except ValueError as e:
                errors.append({
                    "row": line_no,
                    "reason": f"invalid numeric value: {e}",
                    "raw": row,
                })
                continue
            notes = (row.get("notes") or "").strip() or None
            entries.append(RepForecastEntry(
                owner_name=rep_name,
                quarter=quarter,
                commit_acv=commit_acv,
                best_case_acv=best_case_acv,
                notes=notes,
            ))
    return entries, errors
