"""Per-rep Friday scorecards. D10 build; stub until then."""
from __future__ import annotations

from typing import Any


async def for_rep(rep_email: str) -> dict[str, Any]:
    return {
        "text": f"sales_reps: scorecard stub — {rep_email}. Full build D10.",
        "rep_email": rep_email,
        "stub": True,
    }
