"""Weekly SDR + AE leaderboards. D10 build; stub until then."""
from __future__ import annotations

from typing import Any


async def snapshot(kind: str = "ae", week: str | None = None) -> dict[str, Any]:
    return {
        "text": f"sales_reps: leaderboard stub — {kind}, week={week or 'current'}. Full build D10.",
        "kind": kind,
        "week": week,
        "stub": True,
    }
