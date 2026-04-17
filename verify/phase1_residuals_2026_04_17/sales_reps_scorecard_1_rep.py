"""Sales Reps scorecard dry-run for 1 rep — Phase 1 residual.

Scorecards are local-DB reads (call_grades table), no SF / Fireflies required.
Run for a sandbox rep email and capture the rendered Slack payload.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("SLACK_DEV_GUARD", "1")
os.environ.setdefault("SLACK_TEST_CHANNEL", "U08K2UTG3G8")

# Use sandbox SEED data owner if present; harmless if DB has no rows (returns empty scorecard).
TEST_REP = "dan.varela@tryloop.ai.invalid"


async def main() -> int:
    from agents.sales_reps import scorecards

    result = await scorecards.for_rep(TEST_REP)
    artifact = Path(__file__).parent / f"sales_reps_scorecard_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    artifact.write_text(json.dumps(result, indent=2, default=str))
    print(f"artifact: {artifact}")
    print(f"rep: {TEST_REP}")
    text = result.get("text", "")
    for line in text.split("\n")[:12]:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
