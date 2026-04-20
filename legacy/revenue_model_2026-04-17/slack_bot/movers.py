"""Deal mover detection — compares today's pipeline to yesterday's snapshot."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

SNAPSHOT_DIR = Path(__file__).parent / "data"


@dataclass
class DealMove:
    """A single deal that changed stage between snapshots."""
    opp_name: str
    owner: str
    acv: float
    old_stage: str
    new_stage: str
    move_type: str  # "new_win", "new_loss", "advance", "regression", "new_deal"


def detect_movers(
    current_df: pd.DataFrame,
    yesterday_df: pd.DataFrame | None,
) -> list[DealMove]:
    """Compare current pipeline to yesterday's snapshot and return stage changes.

    Joins on (opp_name, owner) to find deals present in both snapshots.
    Returns list of DealMove objects sorted by move_type priority.
    """
    if yesterday_df is None or yesterday_df.empty:
        return []

    movers: list[DealMove] = []

    # Build lookup from yesterday: (opp_name, owner) -> stage
    yesterday_lookup: dict[tuple[str, str], str] = {}
    for _, row in yesterday_df.iterrows():
        key = (str(row.get("opp_name", "")), str(row.get("owner", "")))
        yesterday_lookup[key] = str(row.get("stage", ""))

    # Build set of current keys for new-deal detection
    current_keys: set[tuple[str, str]] = set()

    for _, row in current_df.iterrows():
        opp_name = str(row.get("opp_name", ""))
        owner = str(row.get("owner", ""))
        acv = float(row.get("acv", 0))
        new_stage = str(row.get("stage", ""))
        key = (opp_name, owner)
        current_keys.add(key)

        if key in yesterday_lookup:
            old_stage = yesterday_lookup[key]
            if old_stage == new_stage:
                continue  # No change

            # Determine move type
            if new_stage == "Closed Won":
                move_type = "new_win"
            elif new_stage == "Closed Lost":
                move_type = "new_loss"
            elif old_stage == "Closed Won" or old_stage == "Closed Lost":
                move_type = "advance"  # Reopened deal
            else:
                # Use stage ordering heuristic: later stages are advances
                move_type = "advance"  # Default; could refine with stage order
        else:
            # Deal exists today but not yesterday — new deal
            move_type = "new_deal"

        if key not in yesterday_lookup or yesterday_lookup[key] != new_stage:
            old = yesterday_lookup.get(key, "")
            movers.append(DealMove(
                opp_name=opp_name,
                owner=owner,
                acv=acv,
                old_stage=old,
                new_stage=new_stage,
                move_type=move_type,
            ))

    # Sort by priority: wins first, then losses, advances, new deals
    priority = {"new_win": 0, "new_loss": 1, "advance": 2, "regression": 3, "new_deal": 4}
    movers.sort(key=lambda m: (priority.get(m.move_type, 99), -m.acv))

    return movers


def save_snapshot(df: pd.DataFrame, path: Path | None = None) -> Path:
    """Save current pipeline data as JSON snapshot for tomorrow's comparison.

    Persists only the columns needed for mover detection.
    """
    if path is None:
        path = SNAPSHOT_DIR / "yesterday.json"

    path.parent.mkdir(parents=True, exist_ok=True)

    cols = ["opp_name", "owner", "stage", "acv", "organization"]
    available = [c for c in cols if c in df.columns]
    snapshot = df[available].copy()

    records = snapshot.to_dict(orient="records")
    path.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
    return path


def load_snapshot(path: Path | None = None) -> pd.DataFrame | None:
    """Load yesterday's pipeline snapshot from JSON.

    Returns None if the file doesn't exist.
    """
    if path is None:
        path = SNAPSHOT_DIR / "yesterday.json"

    if not path.exists():
        return None

    try:
        records = json.loads(path.read_text(encoding="utf-8"))
        if not records:
            return None
        return pd.DataFrame(records)
    except (json.JSONDecodeError, ValueError):
        return None
